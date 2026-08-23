# Broker Team Guide

You are the market maker. You connect to Alpaca for real prices, then post
bid/ask quotes on the exchange.  Your edge is the spread you earn; your risk
is the inventory you accumulate.

---

## What you build

You run a **market-making bot** that:

1. Subscribes to Alpaca's paper-trading WebSocket for real-time equity and crypto prices.
2. Continuously posts a bid (buy) and an ask (sell) on the exchange, centered on the Alpaca mid.
3. Earns the spread when both sides fill.
4. Manages inventory to avoid being too long or too short any single name.

The Alpaca connection and exchange quoting scaffolding are already in `broker/broker.py`.  
Your job is to tune the parameters and implement the progressively harder TODOs.

---

## How to run it

```bash
export ALPACA_API_KEY=your_key
export ALPACA_API_SECRET=your_secret
make broker
# or: python -m broker.broker
```

Without Alpaca keys the bot starts but won't quote (it has no prices).  
For offline testing use `make sim` — the simulated exchange provides synthetic prices.

---

## Configuration (`broker/config.py`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `TEAM_ID` | `"broker_alpha"` | Your team name on the leaderboard |
| `EXCHANGE_URL` | `ws://localhost:8765` | Exchange address |
| `BASE_SPREAD` | `0.30` | Fixed $ spread (Level 1) |
| `MAX_SPREAD_BPS` | `25` | …but never wider than this fraction of the price |
| `MIN_SPREAD_ABS` | `0.02` | …and never tighter than this in dollars |
| `QUOTE_SIZE` | `5` | Shares per side per quote |
| `REQUOTE_THRESHOLD_BPS` | `5` | Requote when the centre drifts this many bps |
| `REQUOTE_THRESHOLD` | `0.01` | Absolute $ floor for the above |
| `QUOTE_MAX_AGE_SEC` | `30` | Refresh a quote this old even in a flat market |
| `REFERENCE_HALFLIFE` | `45` | Seconds to close half the gap to the Yahoo reference |

**What you quote around.** Not your own book mid — that is an echo of your own
quotes. The centre is the venue's published mark, `BookSnapshot.ref_price`
(microprice + shared fundamental + live trade impact), pulled slowly toward
Yahoo. Your bot has no random walk of its own: quotes move when the market
moves. (`INTRADAY_SIGMA` was removed in Aug 2026; it still parses and is
ignored with a warning.)

---

## Unlock levels

### Level 1 — Static spread quoting *(start here)*
- Post a fixed `BASE_SPREAD` around the venue mark (`BookSnapshot.ref_price`)
- Verify: connect to the exchange, place a quote, confirm `OrderAck` arrives

### Level 2 — Event-driven requoting
- The shipped loop wakes every `REQUOTE_INTERVAL_SEC` and requotes what moved
  more than `REQUOTE_THRESHOLD_BPS`. It is still up to half a cycle late, and
  it replaces BOTH sides even when only one is wrong.
- Drive requotes from the `BookSnapshot` handler instead, and amend one side
  at a time so the untouched side keeps its queue position and the book is
  never left one-sided
- Hint: look for the `TODO Level 2` block in `requote_loop()`
- Verify: measure fills per message sent before and after, and how often you
  are top of book when a taker arrives

### Level 3 — Volatility-adjusted spread
- Compute a rolling standard deviation of `price_history` (window = `VOL_WINDOW` ticks)
- Set `spread = BASE_SPREAD + VOL_MULTIPLIER × vol_estimate`
- Wide spread when volatile; narrow spread when quiet — maximises risk-adjusted income
- Hint: look for `TODO Level 3` in `compute_spread()`
- Verify: fire the "earnings" shock, confirm spread widens automatically

### Level 4 — Inventory management and quote skew
- If you are long N shares, skew your quotes downward to attract sell flow
- `skew = net_position × SKEW_FACTOR`
- `bid = mid - spread/2 + skew`,  `ask = mid + spread/2 + skew`
- Cap `net_position` at `MAX_POSITION` — refuse to quote if the limit is reached
- Hint: look for `TODO Level 4` in `compute_skew()`
- Verify: accumulate a long position, confirm your ask moves below mid

### Level 5 — Toxic flow detection
- Track which trader IDs consistently cause adverse fills (price moves against you after fill)
- Widen the spread or stop quoting when `toxicity_score[trader_id] > TOXICITY_THRESHOLD`
- Hint: look for `TODO Level 5` in `is_toxic()` and `_on_trade()`
- Verify: run the momentum strategy bot against yourself; confirm widening

### Level 6 — Multi-exchange routing
- Connect to two exchanges simultaneously
- Route quotes to whichever exchange has the highest fill rate for a given symbol
- Verify: run two exchange instances, confirm fills split between them

---

## Scoring

Your score = spread income − inventory losses:

- **Spread income** = (ask fill price − bid fill price) × quantity / 2 per round trip
- **Inventory loss** = net position × adverse price move (can go very negative)
- **Net worth** = cash + mark-to-market positions (marked to Alpaca mid at session close)

The broker with the highest net worth at session close wins.  
Being flat (zero inventory) at the close is usually better than holding a position.
