# AlgoArena — Architecture Reference

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Component Map](#2-component-map)
3. [Connection Lifecycle](#3-connection-lifecycle)
4. [Message Protocol](#4-message-protocol)
5. [Matching Engine](#5-matching-engine-clob)
6. [Plugin Architecture](#6-plugin-architecture)
7. [Adding a New Exchange](#7-adding-a-new-exchange)
8. [Adding a New Broker](#8-adding-a-new-broker)
9. [Adding a New Trader](#9-adding-a-new-trader)
10. [Adding New Securities and Shocks](#10-adding-new-securities-and-shocks)
11. [Data Flow Reference](#11-data-flow-reference)

---

## 1. System Overview

AlgoArena is a networked trading competition. Three teams run independent
Python processes that communicate exclusively over WebSockets:

```
                        ┌─────────────────────────────────────────┐
                        │             EXCHANGE SERVER              │
                        │                                          │
   Yahoo ──► Broker ───►│  OrderBook  ──►  Portfolio tracker       │
 (yfinance)             │                                          │
                        │      ▲                  │                │
              Trader ───►│  matching    ◄──  ArenaRegistry          │
                        │      │            (plugins)              │
   Teacher ─────────────►│  price tick  ──►  leaderboard           │
   Dashboard (observer) │                                          │
                        └──────────────────────────────────────────┘
```

**Exchange** is the server. Everyone else is a client.

| Role | What it does | Scores on |
|------|-------------|-----------|
| Exchange | Runs the CLOB, collects fees | Fee revenue + uptime |
| Broker | Posts bid/ask quotes using Yahoo Finance prices | Spread income − inventory risk |
| Trader | Runs an algorithmic strategy | Net worth at session close |

There is no shared database, no REST API, and no message broker.
All state lives in the exchange server's memory. Clients are stateless —
if a trader process restarts it reconnects and continues from where the
exchange left off (portfolios survive reconnects within a session).

---

## 2. Component Map

```
algoarena/
│
├── shared/                  ← The contract. Imported by everyone.
│   ├── messages.py          ← All Pydantic message schemas + parse_message()
│   ├── orderbook.py         ← CLOB matching engine (pure logic, no I/O)
│   ├── auth.py              ← Team token auth (hosted deployments)
│   └── roster.py            ← Team roster loading/validation
│
├── plugins/                 ← The registry. Never talks to the network.
│   ├── __init__.py          ← ArenaRegistry class + global `arena` instance
│   ├── securities/defaults.py  ← shared deterministic fundamental per symbol
│   ├── securities/futures.py   ← ARENA10 index future
│   ├── shocks/defaults.py   ← Shock event functions
│   └── strategies/examples.py  ← Example signal functions
│
├── exchange/                ← The exchange engine
│   ├── server.py            ← WebSocket server, Portfolio, price tick loop
│   ├── config.py            ← HOST, PORT, fee schedules, feature flags
│   ├── price_engine.py      ← Venue mark: microprice + fundamental blend
│   ├── ipo.py               ← Primary market: bookbuild → pricing →
│   │                           allocation → listing
│   ├── circuit_breaker.py   ← LULD bands, halts, SSR
│   ├── scenario.py          ← Week scenario loader (teacher/season/*.json)
│   ├── scoring.py           ← Season scoring and leaderboard math
│   ├── seats.py, upgrades.py, calendar.py, limits.py
│   └── persistence.py       ← Season state → data/season.json
│
├── broker/                  ← Broker engine (reference market maker)
│   ├── broker.py            ← yfinance polling feed, cancel-then-requote loop
│   └── config.py            ← TEAM_ID, EXCHANGE_URL, YAHOO_POLL_INTERVAL,
│                               spread params
│
├── trader/                  ← Trader engine + bot templates
│   ├── trader.py            ← MarketData, Portfolio, RiskManager, Strategy
│   ├── config.py            ← TEAM_ID, EXCHANGE_URL, risk params
│   └── arb_trader.py, shock_trader.py, flow_bots.py  ← house bots
│
├── arena/                   ← Student SDK — thin hook layer over the engine
│   ├── trader.py            ← on_tick / on_fill / on_event / on_ipo
│   ├── broker.py            ← spread / skew / toxic hooks
│   └── exchange.py          ← exchange policy hooks
│
├── team/                    ← Per-team starter (generated, git-ignored)
├── students/                ← Registered team packages (one per team)
│
├── sim/                     ← Headless season simulator (no network)
│
├── teacher/                 ← Teacher-only tools (not graded)
│   ├── web_dashboard.py     ← Browser dashboard (HTTP + WS relay) — primary UI
│   ├── portal.py, registration.py  ← Student registration
│   ├── season/              ← week01.json … week10.json scenarios
│   ├── teams.json           ← Seed roster (mutated at runtime)
│   ├── dashboard.py         ← Terminal Rich dashboard (deprecated)
│   └── shock_tool.py        ← Interactive shock injector (deprecated)
│
├── scripts/                 ← Ops tooling: create_team, launch_bots,
│                               load_test, replay_session, grade_export, …
├── deploy/                  ← Hosted deploy kit (systemd + Caddy + backups)
└── data/, sessions/         ← Runtime state and recordings (git-ignored)
```

**Dependency rules (the layer cake):**

- `shared/` has no imports from the rest of the project.
- `plugins/` imports only from `shared/`.
- `exchange/`, `broker/`, `trader/` are **the engine**: they import from
  `shared/` and `plugins/`, never from each other's process internals.
- `arena/` wraps the engine for students — it imports `exchange/`, `broker/`,
  and `trader/` and exposes them as small hook classes.
- `team/` and `students/` sit on top of `arena/` (as do `sim/` and
  `scripts/create_team.py`, which drive the engine directly).

---

## 3. Connection Lifecycle

Every client follows the same four-step protocol:

```
Client                                  Exchange
  │                                         │
  │  TCP connect to ws://host:8765          │
  ├────────────────────────────────────────►│
  │                                         │
  │  Handshake {team_id, role, level}       │
  ├────────────────────────────────────────►│  register Portfolio (non-teacher roles)
  │                                         │
  │◄─────── BookSnapshot × N symbols ───────┤  send current book state
  │                                         │
  │         (session loop)                  │
  │◄──── BookSnapshot every 1 s ────────────┤  broadcast to all clients
  │◄──── Leaderboard every 5 s ─────────────┤  broadcast to all clients
  │◄──── SessionEvent (OPEN / CLOSED) ──────┤  triggered by teacher
  │                                         │
  │  PlaceOrder / CancelOrder               │  broker and trader send these
  ├────────────────────────────────────────►│
  │◄──── OrderAck ──────────────────────────┤
  │◄──── TradeExecution (if filled) ────────┤  sent to buyer and seller only
  │◄──── PortfolioUpdate ───────────────────┤
  │                                         │
  │  TeacherCommand {open_session, ...}     │  teacher role only
  ├────────────────────────────────────────►│
  │                                         │
  │  TCP disconnect                         │
  ├────────────────────────────────────────►│  Portfolio remains in exchange memory
```

**Key invariants:**

- The first message from any client must be a `Handshake`. The connection is
  dropped if it does not arrive within 15 seconds.
- Portfolios survive disconnects. A trader that crashes and reconnects picks
  up its existing cash and positions.
- Teachers get no portfolio and do not appear on the leaderboard.
- `TradeExecution` is sent only to the two counterparties, not broadcast.
- `BookSnapshot` and `Leaderboard` are broadcast to everyone including observers.

---

## 4. Message Protocol

### The golden rule

**Never send raw dicts.** Always use `model.model_dump_json()` to send,
and `parse_message(json.loads(raw))` to receive. This is enforced at the
type level — `parse_message` raises `ValidationError` on any schema violation.

### Client → Exchange messages

| Message | Sender | Purpose |
|---------|--------|---------|
| `Handshake` | everyone | Authenticate and declare role |
| `PlaceOrder` | broker, trader | Submit a limit / market / IOC order |
| `CancelOrder` | broker, trader | Cancel a resting order by ID |
| `TeacherCommand` | teacher only | Control session (open, close, shock) |

### Exchange → Client messages

| Message | Recipient | Purpose |
|---------|-----------|---------|
| `OrderAck` | order sender | Confirm order was accepted + its ID |
| `TradeExecution` | buyer + seller | Fill notification with price, qty, fee |
| `BookSnapshot` | broadcast | Current bids/asks, mid price, spread |
| `PortfolioUpdate` | individual | Current cash, positions, P&L |
| `Leaderboard` | broadcast | All teams ranked by net worth |
| `SessionEvent` | broadcast | SESSION_OPEN, SESSION_CLOSED, etc. |
| `ErrorMsg` | individual | Rejection or parse error |

### How discriminated union parsing works

Every schema has a `type` field with a `Literal["..."]` value:

```python
class PlaceOrder(BaseModel):
    type: Literal["place_order"] = "place_order"   # ← discriminator
    team_id: str
    ...
```

`parse_message()` reads `raw["type"]`, looks it up in `_TYPE_MAP`, and
validates the full payload:

```python
def parse_message(raw: dict) -> BaseModel:
    model_cls = _TYPE_MAP[raw["type"]]   # KeyError if unknown
    return model_cls.model_validate(raw) # ValidationError if wrong shape
```

To add a new message type you must:

1. Define it in `shared/messages.py` with a unique `Literal` type string.
2. Add it to `_TYPE_MAP`.
3. Add it to `AnyClientMessage` or `AnyExchangeMessage` union.
4. Add it to `__all__`.
5. Handle it in `exchange/server.py` (if it's a client → exchange message).

---

## 5. Matching Engine (CLOB)

`shared/orderbook.py` is a **pure function library** — no I/O, no async,
no global state. The exchange instantiates one `OrderBook` per symbol.

### Price-time priority

```
Incoming BUY limit at $182.60

Resting asks (sorted lowest first):
  $182.50  qty=5   ← best ask: crosses ($182.60 ≥ $182.50) → FILL at $182.50
  $182.65  qty=3   ← next: doesn't cross
  $182.70  qty=2

Result: Trade at $182.50 for min(incoming.qty, 5)
        Remaining incoming quantity rests as a new bid at $182.60
```

Rules:
- Trades always execute at the **resting** order's price (not the incoming price).
- Limit orders rest in the book if not fully filled.
- Market orders fill at any price; remainder is cancelled.
- IOC orders fill at limit price or better; remainder is cancelled.
- Fee = `fee_rate × notional`, split 50/50 between buyer and seller.

### OrderBook API

```python
book = OrderBook("AAPL", fee_rate=0.001)

order, trades = book.place_order(
    team_id="my_team",
    side="buy",           # "buy" | "sell"
    price=182.60,
    quantity=10,
    order_type="limit",   # "limit" | "market" | "ioc"
)

cancelled = book.cancel_order(order_id, team_id)  # returns Order or None

bid = book.best_bid()    # float | None
ask = book.best_ask()    # float | None
mid = book.mid_price()   # float | None
spd = book.spread()      # float | None
imb = book.order_book_imbalance()   # -1.0 … +1.0
snap = book.get_snapshot(depth=10)  # {bids, asks, mid_price, spread, total_volume}
```

The exchange calls `book.place_order()` and then iterates over the returned
`trades` list to update both counterparties' portfolios:

```python
order, trades = self.books[symbol].place_order(...)
for trade in trades:
    buyer_portfolio.apply_buy(trade.symbol, trade.price, trade.quantity, trade.fee / 2)
    seller_portfolio.apply_sell(trade.symbol, trade.price, trade.quantity, trade.fee / 2)
    await self._broadcast_trade(trade)
```

---

## 6. Plugin Architecture

The plugin system is the heart of AlgoArena's extensibility. Three types of
plugins can be registered: **securities**, **shocks**, and **strategies**.
All three are stored in a single global `ArenaRegistry` instance (`arena`)
in `plugins/__init__.py`.

### Why a registry?

Without a registry you would hard-code securities into the exchange, hard-code
shocks into the teacher tool, and hard-code strategies into the trader. Adding
NVDA would mean editing three files. With the registry, you add NVDA in one
place and everything else picks it up automatically on import.

### The global arena instance

```python
# plugins/__init__.py
arena = ArenaRegistry()   # one instance, shared by all modules

# Any module that wants to register a plugin does:
from plugins import arena
arena.register_security(...)
```

Plugins register themselves on **import**. The exchange imports the defaults
at startup; tests import them directly; students can add their own by simply
importing their module.

### Security plugin

A security is a symbol with a **price function**:

```python
def price_fn(prev_price: float, tick: int, params: dict) -> float:
    """Return the new price for this tick."""
    ...
```

Register it:

```python
from plugins import arena

arena.register_security(
    id="NVDA",
    name="NVIDIA Corp.",
    asset_type="equity",
    base_price=875.00,
    color="#22c55e",
    price_fn=my_price_fn,
    vol=2.5,
)
```

The exchange calls `arena.tick_prices(tick)` every second, which calls every
registered price function and updates `arena.prices`. Exceptions inside a
price function are caught and logged — a broken plugin never crashes the engine.

### Shock plugin

A shock is a one-time event that overrides prices:

```python
def apply_fn(prices: dict[str, float],
             securities: list[SecurityDef],
             params: dict) -> ShockResult:
    """Modify prices and return the result."""
    new_prices = dict(prices)
    new_prices["AAPL"] *= 0.90   # 10% drop
    return ShockResult(
        prices=new_prices,
        message="AAPL flash crash: −10%",
        affected=["AAPL"],
    )

arena.register_shock(
    id="aapl_crash",
    label="AAPL Flash Crash",
    description="Drops AAPL 10% instantly.",
    category="equity",
    apply_fn=apply_fn,
)
```

The teacher fires a shock via `arena.apply_shock("aapl_crash")` or via the
web dashboard (which sends a `TeacherCommand` to the exchange, which calls
`arena.apply_shock()` and broadcasts a `SessionEvent`).

### Strategy plugin

A strategy is a signal-generating function:

```python
def signal_fn(
    symbol: str,
    prices: dict[str, float],
    history: list[float],
    book: OrderBook | None,
    portfolio: dict,
) -> Signal | None:
    """Return a Signal to trade, or None to pass."""
    ...

arena.register_strategy(
    id="my_strategy",
    name="My Strategy",
    description="Buys low, sells high.",
    color="#60a5fa",
    signal_fn=signal_fn,
)
```

Strategy plugins are used by the `SimSession` (offline testing) and can be
called directly by traders. `arena.get_signal()` wraps the call in a
try/except so a crashing strategy never takes down the engine.

---

## 7. Adding a New Exchange

An exchange is a WebSocket server that:

1. Accepts `Handshake` → `PlaceOrder` / `CancelOrder` / `TeacherCommand`
2. Broadcasts `BookSnapshot`, `Leaderboard`, `SessionEvent`
3. Sends `OrderAck`, `TradeExecution`, `PortfolioUpdate` to individuals

The simplest path is to modify `exchange/server.py`. If you want to run a
**second independent exchange** (Level 6 multi-exchange routing), create a
new file and change the port:

### Minimal custom exchange

```python
# exchange/my_exchange.py
import asyncio, json
import websockets
from shared.messages import Handshake, PlaceOrder, OrderAck, parse_message
from shared.orderbook import OrderBook

SYMBOLS = ["AAPL", "TSLA"]
PORT = 8766   # different port from the default exchange

books = {sym: OrderBook(sym, fee_rate=0.001) for sym in SYMBOLS}

async def handle(ws):
    raw = await ws.recv()
    hs = parse_message(json.loads(raw))   # must be Handshake
    assert isinstance(hs, Handshake)

    async for raw_msg in ws:
        msg = parse_message(json.loads(raw_msg))
        if isinstance(msg, PlaceOrder):
            book = books[msg.symbol]
            order, trades = book.place_order(
                team_id=msg.team_id, side=msg.side,
                price=msg.price, quantity=msg.quantity,
                order_type=msg.order_type,
            )
            ack = OrderAck(
                order_id=order.order_id, team_id=msg.team_id,
                symbol=msg.symbol, side=msg.side,
                price=msg.price, quantity=msg.quantity,
            )
            await ws.send(ack.model_dump_json())

async def main():
    async with websockets.serve(handle, "localhost", PORT):
        await asyncio.Future()

asyncio.run(main())
```

### What the real exchange adds

On top of this skeleton, `exchange/server.py` adds:

| Feature | Location in server.py |
|---------|----------------------|
| Portfolio accounting | `Portfolio.apply_buy()` / `apply_sell()` |
| Broadcast book snapshots every 1 s | `_broadcast_book_snapshots()` |
| Broadcast leaderboard every 5 s | `_broadcast_leaderboard()` |
| Price tick loop | `_price_tick_loop()` calls `arena.tick_prices()` |
| Shock injection | `_handle_teacher_command()` calls `arena.apply_shock()` |
| Session open / close | `_open_session()` / `_close_session()` |
| Reconnect support | Portfolios keyed by `team_id`, not by socket |

### Fee model extension (Level 3)

The `OrderBook` splits fees 50/50 by default. To implement maker/taker fees,
subclass `OrderBook` and override `_match()`, or add the fee logic in
`exchange/server.py` after `book.place_order()` returns:

```python
# After place_order returns trades:
for trade in trades:
    if trade.aggressor == "buy":
        maker_fee = -config.MAKER_REBATE * trade.price * trade.quantity
        taker_fee =  config.TAKER_FEE   * trade.price * trade.quantity
    else:
        maker_fee = -config.MAKER_REBATE * trade.price * trade.quantity
        taker_fee =  config.TAKER_FEE   * trade.price * trade.quantity
    buyer_portfolio.apply_buy(..., taker_fee if trade.aggressor=="buy" else maker_fee)
    seller_portfolio.apply_sell(..., ...)
```

---

## 8. Adding a New Broker

A broker is a WebSocket **client** that connects to the exchange, reads prices
from somewhere (Yahoo Finance or a custom feed), and continuously posts bid/ask pairs.

### Minimal custom broker

```python
# my_broker.py
import asyncio, json
import websockets
from shared.messages import Handshake, PlaceOrder, parse_message

EXCHANGE_URL = "ws://localhost:8765"
TEAM_ID      = "my_broker"
SPREAD       = 0.20    # $0.20 between bid and ask
SIZE         = 10

async def main():
    async with websockets.connect(EXCHANGE_URL) as ws:
        # 1. Identify ourselves
        hs = Handshake(team_id=TEAM_ID, role="broker", level=1)
        await ws.send(hs.model_dump_json())

        # 2. Wait for SESSION_OPEN, then quote
        mid = 182.50   # replace with a real Yahoo Finance price

        while True:
            bid_order = PlaceOrder(
                team_id=TEAM_ID, symbol="AAPL", side="buy",
                order_type="limit", price=mid - SPREAD/2, quantity=SIZE,
            )
            ask_order = PlaceOrder(
                team_id=TEAM_ID, symbol="AAPL", side="sell",
                order_type="limit", price=mid + SPREAD/2, quantity=SIZE,
            )
            await ws.send(bid_order.model_dump_json())
            await ws.send(ask_order.model_dump_json())
            await asyncio.sleep(2.0)

asyncio.run(main())
```

### Plugging in a real feed (the reference broker)

The full `broker/broker.py` polls Yahoo Finance in a **daemon thread**
(yfinance is blocking HTTP with no WebSocket API, so it stays off the asyncio
loop). The thread writes into a plain dict; the async requote loop reads it:

```python
def start_yahoo_feeds(self):
    self._loop = asyncio.get_event_loop()
    threading.Thread(
        target=self._yahoo_poll_loop,
        args=(config.EQUITY_SYMBOLS,),
        daemon=True, name="yahoo-feed",
    ).start()

def _yahoo_poll_loop(self, symbols):
    import yfinance as yf
    while True:
        for sym in symbols:
            price = float(yf.Ticker(sym).fast_info.last_price)
            if price > 0:
                self.state.yahoo_prices[sym] = price
        time.sleep(config.YAHOO_POLL_INTERVAL)
```

No API keys are needed, and `fast_info.last_price` returns the last traded
price even when markets are closed — the game works on weekends. To swap in
any other data source (a paid feed, a recorded file, another venue), replace
the poll loop: anything that keeps `state.yahoo_prices` fresh will do.

### Adding a second broker with a different strategy

1. Copy `broker/broker.py` to e.g. `broker/aggressive_broker.py`.
2. Change `TEAM_ID` in config or pass it via environment variable.
3. Override `compute_spread()` to return a tighter spread.
4. Override `compute_skew()` to be more aggressive with inventory.
5. Run it on the same exchange — the order book merges all quotes.

Multiple brokers on the same exchange compete for flow. The broker with the
tightest spread that isn't picked off by informed traders wins.

### Broker state machine

```
start_yahoo_feeds()         ← starts the yfinance polling thread
        │
connect_to_exchange()       ← opens WebSocket, sends Handshake
        │
    ┌───┴──────────────────────────────────────┐
    │  asyncio.gather(listen_to_exchange(),     │
    │                 requote_loop())           │
    │                                          │
    │  listen_to_exchange():                   │
    │    OrderAck      → store resting order ID│
    │    TradeExecution→ clear filled slot     │
    │    PortfolioUpdate→ sync cash/positions  │
    │    SessionEvent  → set session_open flag │
    │                                          │
    │  requote_loop() every 2 s:               │
    │    for each symbol:                      │
    │      cancel old bid + ask                │
    │      place new bid at mid - spread/2     │
    │      place new ask at mid + spread/2     │
    └──────────────────────────────────────────┘
        │
SESSION_CLOSED → flatten_all() → cancel quotes + market-sell inventory
```

---

## 9. Adding a New Trader

A trader is a WebSocket client that implements a strategy. The template in
`trader/trader.py` provides four classes that you extend.

### Minimal custom trader

```python
# my_trader.py
import asyncio, json
import websockets
from shared.messages import Handshake, PlaceOrder, BookSnapshot, parse_message

EXCHANGE_URL = "ws://localhost:8765"
TEAM_ID      = "my_trader"

async def main():
    async with websockets.connect(EXCHANGE_URL) as ws:
        hs = Handshake(team_id=TEAM_ID, role="trader", level=1)
        await ws.send(hs.model_dump_json())

        async for raw in ws:
            msg = parse_message(json.loads(raw))
            if isinstance(msg, BookSnapshot):
                # Simple strategy: always buy 1 share at mid price
                if msg.mid_price > 0:
                    order = PlaceOrder(
                        team_id=TEAM_ID, symbol=msg.symbol,
                        side="buy", order_type="limit",
                        price=msg.mid_price, quantity=1,
                    )
                    await ws.send(order.model_dump_json())

asyncio.run(main())
```

### Using the full template (trader/trader.py)

The template decomposes the trader into four cooperating classes:

```
TraderBot
  ├── MarketData   — maintains live view of all order books and price history
  ├── Portfolio    — tracks cash, positions, realized/unrealized P&L
  ├── RiskManager  — pre-trade checks (position limits, loss halt, stop-loss)
  └── Strategy     — generates Signal objects from market data
```

**Signal flow in `trading_loop()`:**

```python
while session_open:
    signal = strategy.generate_signal(market, portfolio)  # your code
    if signal:
        ok, reason = risk.check_order(signal, portfolio, market)
        if ok:
            await _place_order(signal)
    await asyncio.sleep(TICK_INTERVAL_SEC)
```

### Implementing a new strategy

Edit `Strategy.generate_signal()` in `trader/trader.py`. It receives:

| Argument | Type | Contents |
|----------|------|----------|
| `market` | `MarketData` | `.mid_price(sym)`, `.prices(sym)`, `.best_bid(sym)`, `.order_book_imbalance(sym)` |
| `portfolio` | `Portfolio` | `.cash`, `.positions`, `.net_worth(market)`, `.can_buy(sym, qty, price)` |

Return a `Signal` to trade or `None` to pass:

```python
from shared.messages import Signal

def generate_signal(self, market, portfolio) -> Signal | None:
    for sym in config.SYMBOLS:
        prices = market.prices(sym)
        if len(prices) < 20:
            continue   # not enough history yet
        
        # Example: z-score mean reversion
        window = prices[-20:]
        mean = sum(window) / 20
        std  = (sum((x - mean)**2 for x in window) / 19) ** 0.5
        if std == 0:
            continue
        z = (prices[-1] - mean) / std
        
        price = market.mid_price(sym)
        if z > 1.5 and portfolio.can_sell(sym, 10):
            return Signal(symbol=sym, side="sell", quantity=10, price=price)
        if z < -1.5 and portfolio.can_buy(sym, 10, price):
            return Signal(symbol=sym, side="buy",  quantity=10, price=price)
    
    return None
```

### Registering your strategy as a plugin

If you want your strategy to appear in the sim and the web dashboard:

```python
# In your file, after defining the function:
from plugins import arena

arena.register_strategy(
    id="my_mean_rev",
    name="My Mean Reversion",
    description="Z-score fade, 20-tick window, 1.5σ threshold.",
    color="#a78bfa",
    signal_fn=generate_signal,  # bare function, not bound method
)
```

Then import your file anywhere before `SimSession().run()` and it will be
available:

```python
from my_strategy import my_signal   # triggers registration on import
result = SimSession().run(
    strategies=[("my_bot", my_signal)],
    symbols=["AAPL", "SYNTH"],
)
```

---

## 10. Adding New Securities and Shocks

### New security

Add it to `plugins/securities/defaults.py` (or your own file):

```python
from plugins import arena
from plugins.securities.defaults import make_fundamental

arena.register_security(
    id="AMZN",
    name="Amazon.com Inc.",
    asset_type="equity",
    base_price=178.00,
    color="#fb923c",
    price_fn=make_fundamental("AMZN", sigma=0.8),   # 80% annualised vol
    vol=0.8,
)
```

Then add `"AMZN"` to `exchange/config.py → SYMBOLS`.  
The exchange reads `SYMBOLS` at startup and creates an `OrderBook` for each.

**`make_fundamental(symbol, sigma, mu=0.0)` parameters:**

- `symbol` — the security id. It seeds the shared path, so every venue
  computes the same one.
- `sigma` — annualised volatility as a decimal. AAPL uses 0.7 (70%/year),
  which is ~0.03% per one-second tick.
- `mu` — annualised drift (0.1 = 10%/year upward trend).

**Multi-venue coherence is your responsibility in a custom plugin.** Draw
randomness from `random.Random(f"{symbol}:{tick}:{seed}")` — never from the
global `random` module, or each exchange process will walk its own path and
the venues will disagree about what your security is worth. `make_gbm(sigma)`
(the old factory) does exactly that and is kept only for single-venue
experiments. See `plugins/securities/defaults.py` and docs/SEASON_GUIDE.md
*How a price is formed*.

For a **mean-reverting** or **sine-wave** security, use `make_sine()`:

```python
from plugins.securities.defaults import make_sine

arena.register_security(
    id="SYNTH",
    name="Synthetic Sine",
    asset_type="synthetic",
    base_price=100.00,
    color="#e879f9",
    price_fn=make_sine(base_price=100.0, amplitude=10.0, period=3600),
)
```

### New shock

Add it to `plugins/shocks/defaults.py` (or your own file):

```python
from plugins import arena
from shared.messages import ShockResult

def _my_shock(prices, securities, params):
    new_prices = dict(prices)
    # params can contain teacher-supplied values, e.g. params.get("pct", 0.05)
    new_prices["AAPL"] *= (1 + params.get("pct", 0.15))
    return ShockResult(
        prices=new_prices,
        message="AAPL surprise earnings beat: +15%",
        affected=["AAPL"],
    )

arena.register_shock(
    id="aapl_earnings_beat",
    label="AAPL Earnings Beat",
    description="AAPL jumps 15% on surprise earnings beat.",
    category="equity",
    apply_fn=_my_shock,
)
```

The shock immediately appears in the web dashboard's **Teacher Shock Panel**
(the panel reads shocks dynamically from the registry on startup).

---

## 11. Data Flow Reference

### Order placement

```
Trader                    Exchange                    OrderBook
  │                          │                            │
  │  PlaceOrder              │                            │
  ├─────────────────────────►│                            │
  │                          │  book.place_order(...)     │
  │                          ├───────────────────────────►│
  │                          │◄── (order, [trades]) ──────┤
  │                          │                            │
  │◄── OrderAck ─────────────┤                            │
  │                          │  (for each trade)          │
  │◄── TradeExecution ───────┤  buyer_portfolio.apply_buy │
  │◄── PortfolioUpdate ──────┤  seller_portfolio.apply_sell│
  │                          │                            │
Counterparty◄─TradeExecution─┤                            │
Counterparty◄─PortfolioUpdate┤                            │
```

### Price tick (every 1 second)

```
ExchangeServer._price_tick_loop()
  │
  ├─ arena.tick_prices(tick)          # calls each price_fn
  │     └─ for each security:
  │           price_fn(prev, tick, {}) → new_price
  │           arena.prices[sym] = new_price
  │
  ├─ for each symbol:
  │     ref_prices[sym] = book.mid_price() or arena.prices[sym]
  │
  └─ broadcast BookSnapshot to all clients
```

### Session lifecycle

```
Teacher fires "open_session"
  │
  ▼
Exchange._open_session()
  ├─ session_open = True
  └─ broadcast SessionEvent(event="SESSION_OPEN")

All bots receive SESSION_OPEN → begin trading

Teacher fires "close_session" (or time limit)
  │
  ▼
Exchange._close_session()
  ├─ session_open = False
  ├─ broadcast SessionEvent(event="SESSION_CLOSED")
  └─ broadcast final Leaderboard

All bots receive SESSION_CLOSED → flatten positions, stop trading
```

### Offline simulation (no network)

`tests/sim_session.py` replicates the exchange in-process:

```
SimSession.run()
  │
  ├─ SimulatedExchange    — instantiates OrderBook + Portfolio directly
  │     └─ advance_tick() — calls arena's price_fn for each symbol
  │
  ├─ SimulatedBroker      — cancel-then-requote around local prices each tick
  │
  └─ SimulatedTrader(signal_fn)  — calls signal_fn, places market orders
        └─ pnl_curve[]           — net worth recorded every tick
```

Use `SYNTH` (sine wave) for testing — it generates predictable price
movements that reliably trigger strategy signals within a few hundred ticks.
GBM symbols (AAPL, TSLA) move too slowly at 1-second ticks to produce
meaningful P&L curves in short test runs.

---

*Generated from the AlgoArena codebase. If a detail conflicts with the code,
trust the code — it is the source of truth.*
