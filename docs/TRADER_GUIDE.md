# Trader Team Guide

You run the algorithmic trading bot. You read the order book, generate signals,
and place orders to maximise your net worth by session close.

---

## What you build

You implement a **trading strategy** inside `trader/trader.py`.  
The scaffolding is already there — four classes you progressively upgrade:

| Class | Responsibility |
|-------|---------------|
| `MarketData` | Maintains a live view of prices and order books |
| `Portfolio` | Tracks your cash, positions, and P&L |
| `RiskManager` | Enforces position limits and stop-losses |
| `Strategy` | Your signal logic — **this is where you compete** |

Your bot connects to the exchange over WebSocket, receives `BookSnapshot` updates,
and places `PlaceOrder` messages in response.

---

## How to run it

```bash
make trader
# or: python -m trader.trader
```

The bot connects, waits for `SESSION_OPEN`, then starts trading.  
It flattens all positions automatically when `SESSION_CLOSED` arrives.

---

## Configuration (`trader/config.py`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `TEAM_ID` | `"trader_alpha"` | Your team name on the leaderboard |
| `EXCHANGE_URL` | `ws://localhost:8765` | Exchange address |
| `STARTING_CASH` | `100 000` | Initial cash allocation |
| `SYMBOLS` | `["AAPL","TSLA","BTC"]` | Securities to trade |
| `TICK_INTERVAL_SEC` | `1.0` | How often to run your strategy |
| `MAX_POSITION_SIZE` | `20` | Maximum shares per symbol (Level 4) |
| `MAX_DAILY_LOSS` | `5 000` | Daily loss limit in dollars (Level 4) |

---

## Unlock levels

### Level 1 — Connect and place orders *(start here)*
- Connect, receive `BookSnapshot` messages, place a single test order
- Your surface is `MyTrader.on_tick(market, portfolio)` in `team/trader.py`
  (the `arena` SDK handles all plumbing) — return a `Signal` to trade,
  `None` to sit out; try one manual fill via MY TEAM → ORDER TICKET first
- Goal: confirm your `TEAM_ID` appears on the leaderboard

### Level 2 — Track portfolio in real time
- Parse `PortfolioUpdate` messages and update `Portfolio`
- Display your net worth and positions in the terminal
- Goal: your displayed net worth matches the leaderboard exactly

### Level 3 — Implement a real signal
- Write it in `on_tick()`; worked sketches of every option live in
  `engine/trader/trader.py` (`Strategy.generate_signal`, OPTIONS A–G):
  - **Option A**: Moving-average crossover (momentum)
  - **Option B**: Z-score mean reversion
  - **Option C**: Order book imbalance
  - **Option D**: Pairs / relative value
  - **Option E**: Invent your own
- Goal: positive realized P&L before fees over a 30-minute session

### Level 4 — Risk management
- Implement position limits, inventory checks, and daily loss halt in `RiskManager`
- Implement stop-loss exits in `check_stop_loss()`
- Goal: your bot never loses more than `MAX_DAILY_LOSS` in any session

### Level 5 — Execution optimization
- Minimise market impact by slicing large orders (e.g., TWAP or VWAP execution)
- Detect and avoid quoting into stale prices (check spread vs VWAP deviation)
- Goal: average fill price beats the session VWAP by ≥ 0.05%

### Level 6 — Advanced strategies and ML
- Implement VWAP computation using `MarketData.vwap()` (see `TODO Level 6`)
- Train a simple linear model offline and load its weights as constants
- Use `price_history` and `recent_trades` as features
- Goal: positive Sharpe ratio over five independent 30-minute sessions

---

## Strategy options explained

### Option A — Momentum (MA crossover)
Buy when the 5-tick moving average crosses above the 20-tick MA.
Sell on the reverse cross.  Works when trends persist.

### Option B — Mean reversion (Z-score)
Compute a z-score over the last 20 prices.  Buy when z < −1.5 (oversold),
sell when z > +1.5 (overbought).  Works in range-bound markets.

### Option C — Order book imbalance
Measure excess bid vs ask volume.  Trade in the direction of the imbalance.
Works when institutional order flow is visible in the book.

### Option D — Pairs / relative value
Track the ratio of two correlated securities (e.g., AAPL vs TSLA).
Buy the cheap one, sell the expensive one when the ratio diverges.
Works when mean-reversion holds between pairs.

### Option E — Your own
Use `price_history`, `recent_trades`, and any features you can derive.
The `Signal` object accepts a `confidence` field (0–1) you can use for sizing.

---

## The primary market: on_ipo

In IPO weeks (3 and 8) a new symbol lists mid-session. Your SDK hook
(`arena/trader.py`) is:

```python
def on_ipo(self, symbol: str, lo: float, hi: float, shares: int,
           data: dict) -> int | tuple[int, float] | None:
```

Return your **indication of interest**, or `None` to pass:

- a bare quantity — bids the **top** of the range (the sure allocation), or
- `(quantity, max_price)` — bid tighter and risk missing the deal.

One indication per bot, like a real book: resubmitting **replaces** your
previous indication. Cash is only debited if you are allocated at pricing.

The deal lifecycle arrives as events (also visible in `on_event`):

- **`IPO_OPEN`** — the book is open; `data` carries `symbol`, `offer_range`,
  and `shares`. This is when `on_ipo` fires.
- **`IPO_PRICED`** — the deal priced off the book; allocations are pro-rata
  when oversubscribed, and your cash is debited for whatever you got.
- **`IPO_LISTED`** — the symbol starts trading. The price opens at the offer
  and walks toward its hidden value — it may pop above the offer or break
  below it. Momentum vs fade on listing day is your call.

---

## Scoring

Your **net worth at session close** determines your rank:

```
net worth = cash + unrealized P&L
unrealized P&L = sum of (current_price − avg_entry_price) × qty for open positions
```

At `SESSION_CLOSED`, your bot flattens everything.  Open positions are marked to
the Yahoo mid price (not your fill price), so execution quality matters.

**Tips:**
- Fees are 0.1% of notional — overtrading kills P&L faster than bad signals.
- Being flat at the close is free; holding inventory overnight is not scored.
- The broker's spread is your first hurdle — factor it into your signal threshold.
