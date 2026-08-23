# Exchange Team Guide

You run the market. Every trade in AlgoArena passes through your order book.  
Your score depends on the fees you collect and the uptime you maintain.

---

## What you build

You operate a **Central Limit Order Book (CLOB)** over WebSockets.

- Brokers connect and post bid/ask quotes.
- Traders connect and send buy/sell orders.
- Your engine matches orders using **price-time priority** and broadcasts fills.
- You collect a transaction fee on every trade.

The matching logic is already written in `shared/orderbook.py`.  
Your job is to configure it, extend it, and keep it running.

---

## How to run it

```bash
make exchange
# or: python -m exchange.server
```

The server starts on `localhost:8765` by default.  
Override with environment variables:

```bash
EXCHANGE_HOST=0.0.0.0 EXCHANGE_PORT=9000 make exchange
```

Watch the logs — every connection, order, and fill is printed.

---

## Configuration (`exchange/config.py`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOST` | `localhost` | Bind address |
| `PORT` | `8765` | WebSocket port |
| `FEE_RATE` | `0.001` | Flat fee (0.1% of notional) |
| `SYMBOLS` | `["AAPL","TSLA","BTC"]` | Securities to list |
| `INITIAL_CASH` | `100 000` | Starting cash per team |

---

## Unlock levels

### Level 1 — Basic exchange *(start here)*
- CLOB is running, flat fee is collected
- Verify: two teams can connect, place orders, and see fills in the log
- Key file: `exchange/server.py` — read and understand `handle_client()`

### Level 2 — Portfolio tracking and leaderboard
- The exchange maintains cash and position state for every connected team
- `PortfolioUpdate` messages are broadcast after every fill
- `Leaderboard` messages are broadcast every 5 seconds
- Verify: run `make dashboard` and confirm net worth updates in real time

### Level 3 — Maker/taker fee model
- Passive (resting) orders earn a rebate; aggressive (incoming) orders pay a fee
- Change `FEE_RATE` into a pair: `MAKER_REBATE` and `TAKER_FEE`
- Hint: look for the `TODO Level 3` comment in `orderbook.py`
- Verify: makers show negative `total_fees_paid` on the leaderboard

**Your fee schedule is your product.** In the hosted game your venue's
taker/rebate pair is **published to every participant** (dashboard → STATS →
venue fee schedules, or `GET /api/venues`), and traders route to the cheapest
venue that fills them. Set yours from the portal (MY TEAM → MY VENUE) or
`POST /api/venue {token, taker_bps, rebate_bps}`. Bounds keep the game fair:
taker within teacher-set min/max, and your rebate can never exceed
`taker − net_min`, so a venue can't rebate itself into bankruptcy. Undercut
to attract flow, or charge premium and earn more per trade — that trade-off
IS the exchange game.

### Level 4 — Circuit breakers
- Halt trading on a symbol if price moves more than `CIRCUIT_BREAKER_PCT` in one tick
- Resume automatically after `HALT_DURATION_SEC` seconds
- Verify: run `make shock`, fire the "flash crash" shock, confirm trading halts

### Level 5 — Rate limiting and order types
- Reject teams that place more than `MAX_ORDERS_PER_SEC` orders per second
- Support IOC (Immediate-Or-Cancel) orders: fill what you can, cancel the rest
- Verify: write a test that sends 1 000 orders in 1 second and confirms rejection

### Level 6 — Full analytics
- Export a CSV trade log at session close
- Add a `market_depth` field to `BookSnapshot` (full bid and ask ladders)
- Compute and broadcast VWAP per symbol every 30 seconds
- Verify: the teacher dashboard shows VWAP on the price panel

---

## Scoring

Your score = total fees collected minus penalties:
- **+1 point** per dollar of fees collected
- **−10 points** per second of downtime (any connected team disconnects unexpectedly)
- **−5 points** per invalid order accepted (orders that should have been rejected)

Keep the server stable and the fee rate competitive — if you charge too much,
brokers will route flow to a competing exchange.
