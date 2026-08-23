"""
tests/sim_session.py — full end-to-end AlgoArena simulation, no network required.

Runs the exchange, broker, and trader(s) in a single process using direct
function calls instead of WebSockets.  No Alpaca API keys needed.

Usage as a script:
    python -m tests.sim_session
    python tests/sim_session.py

Usage from your own code:
    from tests.sim_session import SimSession
    from plugins.strategies.examples import momentum_signal

    result = SimSession().run(
        n_ticks=500,
        strategies=[("my_bot", momentum_signal)],
        symbols=["AAPL", "TSLA"],
    )
    result.print_summary()
    result.plot_pnl()
    result.to_csv("/tmp/my_session.csv")

Signal function signature (same as plugin registry):
    def my_signal(symbol, prices, history, book, portfolio) -> Signal | None:
        ...
"""

from __future__ import annotations

import csv
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Any

from shared.messages import Signal
from shared.orderbook import OrderBook, Trade
from exchange.server import Portfolio

logger = logging.getLogger(__name__)

SignalFn = Callable[[str, dict, list, Any, dict], Signal | None]


# ─────────────────────────────────────────────────────────────────────────────
# ASCII chart helper
# ─────────────────────────────────────────────────────────────────────────────

def _ascii_chart(
    series: dict[str, list[float]],
    width: int = 72,
    height: int = 14,
    title: str = "",
) -> str:
    """Render an ASCII line chart of one or more P&L series.

    Returns a multi-line string — just print() it.
    """
    all_vals = [v for vals in series.values() for v in vals]
    if not all_vals:
        return "  (no data)\n"

    y_min, y_max = min(all_vals), max(all_vals)
    pad = max(1.0, abs(y_max - y_min) * 0.05)
    y_min -= pad
    y_max += pad
    y_span = y_max - y_min

    MARKERS = ["∙", "○", "◆", "+", "×"]
    grid: list[list[str]] = [[" "] * width for _ in range(height)]

    for s_idx, (_, values) in enumerate(series.items()):
        if not values:
            continue
        mk = MARKERS[s_idx % len(MARKERS)]
        n = len(values)
        prev_r: int | None = None
        for x in range(width):
            vi = min(int(x * n / width), n - 1)
            r = int((y_max - values[vi]) / y_span * (height - 1))
            r = max(0, min(height - 1, r))
            grid[r][x] = mk
            if prev_r is not None:
                lo, hi = sorted([prev_r, r])
                for fill in range(lo + 1, hi):
                    if grid[fill][x] == " ":
                        grid[fill][x] = "│"
            prev_r = r

    lines: list[str] = []
    if title:
        lines.append(f"  {title}")

    for r_idx, row in enumerate(grid):
        y_val = y_max - (r_idx / max(1, height - 1)) * y_span
        lines.append(f"  {y_val:>+10,.0f} │{''.join(row)}")

    lines.append("             └" + "─" * width)
    legend = "   ".join(
        f"{MARKERS[i % len(MARKERS)]} {name}" for i, name in enumerate(series)
    )
    lines.append(f"              {legend}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SimulatedExchange
# ─────────────────────────────────────────────────────────────────────────────

class SimulatedExchange:
    """In-process exchange: matching engine + portfolio accounting.

    Replaces the WebSocket server with direct function calls.
    Each instance has its own independent price state — multiple sim runs
    in the same process don't interfere with each other.
    """

    def __init__(
        self,
        symbols: list[str],
        fee_rate: float = 0.001,
        allow_broker_short: bool = True,
    ) -> None:
        # Load the plugin registry (idempotent — safe to call repeatedly).
        import plugins.securities.defaults  # noqa: F401
        import plugins.shocks.defaults      # noqa: F401
        from plugins import arena

        registered = arena.securities
        self.symbols = [s for s in symbols if s in registered]
        if len(self.symbols) < len(symbols):
            missing = set(symbols) - set(self.symbols)
            logger.warning("SimulatedExchange: symbols not registered: %s", missing)

        # Per-symbol price functions — each instance has its own price state.
        self._price_fns: dict[str, Any] = {
            sym: registered[sym]["price_fn"] for sym in self.symbols
        }
        # Start from the registered base price, not global runtime state.
        self._local_prices: dict[str, float] = {
            sym: registered[sym]["defn"].base_price for sym in self.symbols
        }
        # Reference prices (trade > book mid > GBM tick, same priority as server).
        self.ref_prices: dict[str, float] = dict(self._local_prices)

        self.books: dict[str, OrderBook] = {
            sym: OrderBook(sym, fee_rate=fee_rate) for sym in self.symbols
        }
        self.portfolios: dict[str, Portfolio] = {}
        self.fee_rate = fee_rate
        self.allow_broker_short = allow_broker_short
        self.tick: int = 0
        self.trade_log: list[Trade] = []

    # ------------------------------------------------------------------
    # Team registration
    # ------------------------------------------------------------------

    def register_team(
        self, team_id: str, role: str, level: int = 1
    ) -> Portfolio:
        """Create a portfolio for a new participant (idempotent)."""
        if team_id not in self.portfolios:
            self.portfolios[team_id] = Portfolio(
                team_id=team_id, role=role, level=level
            )
        return self.portfolios[team_id]

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def place_order(
        self,
        team_id: str,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        order_type: str = "limit",
    ) -> tuple:
        """Place an order directly in the matching engine.

        Returns (order, trades).  Returns (None, []) on pre-trade rejection.
        """
        book = self.books.get(symbol)
        portfolio = self.portfolios.get(team_id)
        if book is None or portfolio is None:
            return None, []

        # ── Pre-trade checks ─────────────────────────────────────────
        if side == "buy":
            ref = (
                price if order_type != "market"
                else self.ref_prices.get(symbol, price)
            )
            est_cost = ref * quantity * (1.0 + self.fee_rate / 2)
            if portfolio.cash < est_cost:
                return None, []   # insufficient cash
        else:  # sell
            # Brokers may short-sell in the sim so they can always quote asks.
            if not (self.allow_broker_short and portfolio.role == "broker"):
                held = portfolio.positions.get(symbol, 0)
                if held < quantity:
                    return None, []   # insufficient position

        order, trades = book.place_order(
            team_id=team_id, side=side, price=price,
            quantity=quantity, order_type=order_type,
        )
        for trade in trades:
            self._settle(trade)
        return order, trades

    def cancel_order(
        self, team_id: str, symbol: str, order_id: str
    ) -> bool:
        book = self.books.get(symbol)
        if book is None:
            return False
        return book.cancel_order(order_id, team_id) is not None

    # ------------------------------------------------------------------
    # Trade settlement
    # ------------------------------------------------------------------

    def _settle(self, trade: Trade) -> None:
        buyer_fee = seller_fee = trade.fee / 2

        buyer = self.portfolios.get(trade.buyer_id)
        seller = self.portfolios.get(trade.seller_id)

        if buyer:
            buyer.apply_buy(trade.symbol, trade.price, trade.quantity, buyer_fee)
        if seller:
            seller.apply_sell(trade.symbol, trade.price, trade.quantity, seller_fee)

        # Trade price is the best available ref — highest priority.
        self.ref_prices[trade.symbol] = trade.price
        self.trade_log.append(trade)

    # ------------------------------------------------------------------
    # Time advance
    # ------------------------------------------------------------------

    @property
    def local_prices(self) -> dict[str, float]:
        """Raw GBM/sine prices from the plugin's price_fn.

        This is the equivalent of Alpaca prices in the real system — the
        external truth that drives the broker's requoting.  Always returns
        the most recent price computed by advance_tick(), independent of
        what the order book currently shows.
        """
        return dict(self._local_prices)

    def advance_tick(self) -> dict[str, float]:
        """Advance prices by one tick using each symbol's GBM / sine function.

        ref_prices is updated to the new GBM price every tick so the broker
        and traders see a moving market.  _settle() overrides ref_prices with
        the actual trade price whenever a fill occurs — that is the only time
        the book mid takes precedence.
        """
        self.tick += 1
        for sym in self.symbols:
            prev = self._local_prices[sym]
            try:
                new = max(float(self._price_fns[sym](prev, self.tick, {})), 0.01)
            except Exception:
                new = prev
            self._local_prices[sym] = new
            self.ref_prices[sym] = new   # GBM/sine price is authoritative each tick

        return dict(self.ref_prices)

    # ------------------------------------------------------------------
    # Leaderboard summary
    # ------------------------------------------------------------------

    def leaderboard(self) -> list[dict]:
        rows = []
        for team_id, p in self.portfolios.items():
            nw = p.net_worth(self.ref_prices)
            rows.append({
                "team_id": team_id,
                "role": p.role,
                "cash": round(p.cash, 2),
                "realized_pnl": round(p.realized_pnl, 2),
                "total_fees_paid": round(p.total_fees_paid, 2),
                "net_worth": round(nw, 2),
                "positions": {k: v for k, v in p.positions.items() if v},
            })
        rows.sort(key=lambda x: (x["role"] != "trader", -x["net_worth"]))
        return rows


# ─────────────────────────────────────────────────────────────────────────────
# SimulatedBroker
# ─────────────────────────────────────────────────────────────────────────────

class SimulatedBroker:
    """In-process market maker: quotes a fixed spread around the mid price.

    Requotes every tick by cancelling stale orders and placing fresh ones.
    Uses the SimulatedExchange's GBM prices as the mid reference.
    """

    def __init__(
        self,
        exchange: SimulatedExchange,
        team_id: str = "sim_broker",
        spread: float = 0.30,
        quote_size: int = 5,
    ) -> None:
        self.exchange = exchange
        self.team_id = team_id
        self.spread = spread
        self.quote_size = quote_size

        exchange.register_team(team_id, role="broker")

        # Track resting bid/ask order IDs per symbol.
        self._resting: dict[str, dict] = {
            sym: {"buy_id": None, "sell_id": None}
            for sym in exchange.symbols
        }

    def _cancel_quotes(self, symbol: str) -> None:
        r = self._resting[symbol]
        for key in ("buy_id", "sell_id"):
            if r[key]:
                self.exchange.cancel_order(self.team_id, symbol, r[key])
                r[key] = None

    def _quote_symbol(self, symbol: str) -> None:
        # Use the GBM/sine price (local_prices) as the mid reference, same
        # role as Alpaca prices in the live system.  ref_prices can lag after
        # a trade fill; local_prices is always the freshest computed price.
        mid = self.exchange.local_prices.get(symbol)
        if mid is None:
            return

        self._cancel_quotes(symbol)

        bid = round(mid - self.spread / 2, 4)
        ask = round(mid + self.spread / 2, 4)
        if bid <= 0 or ask <= bid:
            return

        order, _ = self.exchange.place_order(
            self.team_id, symbol, "buy", bid, self.quote_size
        )
        if order:
            self._resting[symbol]["buy_id"] = order.order_id

        order, _ = self.exchange.place_order(
            self.team_id, symbol, "sell", ask, self.quote_size
        )
        if order:
            self._resting[symbol]["sell_id"] = order.order_id

    def tick(self) -> None:
        """Requote all symbols for this tick."""
        for sym in self.exchange.symbols:
            try:
                self._quote_symbol(sym)
            except Exception:
                logger.exception("SimulatedBroker: error quoting %s", sym)


# ─────────────────────────────────────────────────────────────────────────────
# SimulatedTrader
# ─────────────────────────────────────────────────────────────────────────────

class SimulatedTrader:
    """In-process trader: wraps a signal_fn and runs it each tick.

    Records the full P&L curve so SessionResult can plot it.

    The signal_fn signature matches the plugin registry convention:
        signal_fn(symbol, prices, history, book, portfolio) -> Signal | None
    """

    def __init__(
        self,
        exchange: SimulatedExchange,
        team_id: str,
        signal_fn: SignalFn,
        symbols: list[str] | None = None,
    ) -> None:
        self.exchange = exchange
        self.team_id = team_id
        self.signal_fn = signal_fn
        self.symbols = symbols or exchange.symbols

        exchange.register_team(team_id, role="trader")

        # Rolling price history per symbol (fed into signal_fn as `history`).
        self.price_history: dict[str, list[float]] = {s: [] for s in self.symbols}

        # Net-worth recorded at the end of each tick.
        self.pnl_curve: list[float] = []

        # All fills this trader received.
        self.fills: list[Trade] = []

    def tick(self) -> None:
        """Update price history, run strategy, and place the resulting order."""
        portfolio = self.exchange.portfolios[self.team_id]
        prices = dict(self.exchange.ref_prices)

        # Update rolling history.
        for sym in self.symbols:
            if sym in prices:
                self.price_history[sym].append(prices[sym])

        # Snapshot portfolio for the signal function.
        portfolio_dict = {
            "cash": portfolio.cash,
            "positions": dict(portfolio.positions),
            "realized_pnl": portfolio.realized_pnl,
            "net_worth": portfolio.net_worth(prices),
            "total_fees_paid": portfolio.total_fees_paid,
        }

        self.pnl_curve.append(portfolio.net_worth(prices))

        # Ask each symbol for a signal; take the first non-None.
        signal: Signal | None = None
        for sym in self.symbols:
            history = self.price_history[sym]
            book = self.exchange.books.get(sym)
            try:
                signal = self.signal_fn(sym, prices, history, book, portfolio_dict)
            except Exception:
                logger.exception(
                    "SimulatedTrader %s: signal_fn raised on %s", self.team_id, sym
                )
                signal = None
            if signal is not None:
                break

        if signal is None:
            return

        # Use market orders so the simulation generates fills against the broker's
        # resting quotes.  Real traders can choose limit vs market in trader/trader.py;
        # here we care about P&L dynamics, not order-type mechanics.
        _, trades = self.exchange.place_order(
            self.team_id, signal.symbol, signal.side,
            signal.price, signal.quantity, order_type="market",
        )
        self.fills.extend(trades)


# ─────────────────────────────────────────────────────────────────────────────
# SessionResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionResult:
    """Results from a completed SimSession run."""

    n_ticks: int
    symbols: list[str]
    price_history: dict[str, list[float]]
    leaderboard: list[dict]
    traders: list[SimulatedTrader]
    broker: SimulatedBroker
    exchange: SimulatedExchange

    def print_summary(self) -> None:
        """Print a formatted summary of the simulation results."""
        w = 72
        print()
        print("=" * w)
        print(f"  AlgoArena SimSession — {self.n_ticks} ticks")
        print("=" * w)

        # Price changes
        print(f"\n  {'Symbol':<8}  {'Start':>10}  {'End':>10}  {'Change':>10}")
        print(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*10}")
        for sym in self.symbols:
            hist = self.price_history.get(sym, [])
            if len(hist) >= 2:
                start, end = hist[0], hist[-1]
                pct = (end - start) / start * 100 if start else 0.0
                sign = "+" if pct >= 0 else ""
                print(f"  {sym:<8}  {start:>10.4f}  {end:>10.4f}  {sign}{pct:>9.2f}%")

        # Leaderboard
        print(f"\n  {'#':<3}  {'Team':<22}  {'Role':<8}  "
              f"{'Net Worth':>12}  {'Realized PnL':>13}  {'Fees':>8}")
        print(f"  {'─'*3}  {'─'*22}  {'─'*8}  "
              f"{'─'*12}  {'─'*13}  {'─'*8}")
        for rank, entry in enumerate(self.leaderboard, 1):
            nw = entry["net_worth"]
            pnl = entry["realized_pnl"]
            fees = entry["total_fees_paid"]
            sign = "+" if pnl >= 0 else ""
            print(f"  {rank:<3}  {entry['team_id']:<22}  {entry['role']:<8}  "
                  f"${nw:>11,.2f}  {sign}{pnl:>12,.2f}  {fees:>8.2f}")

        # Trade stats
        total_trades = len(self.exchange.trade_log)
        total_vol = sum(t.price * t.quantity for t in self.exchange.trade_log)
        total_fees = sum(t.fee for t in self.exchange.trade_log)
        print(f"\n  Trades: {total_trades}   Volume: ${total_vol:,.2f}   "
              f"Exchange fees: ${total_fees:,.4f}")

        if self.traders:
            avg_fills = sum(len(t.fills) for t in self.traders) / len(self.traders)
            print(f"  Avg fills per trader: {avg_fills:.1f}")

        print("=" * w)

    def plot_pnl(self, width: int = 72, height: int = 14) -> None:
        """Print an ASCII P&L chart (net worth over time) for all traders."""
        if not self.traders:
            print("  (no traders to plot)")
            return

        series = {t.team_id: t.pnl_curve for t in self.traders if t.pnl_curve}
        if not series:
            return

        print()
        print(_ascii_chart(series, width=width, height=height,
                           title="Net Worth Over Time (all traders)"))
        print()

    def to_csv(self, path: str) -> None:
        """Export the full trade log to a CSV file."""
        fieldnames = [
            "trade_id", "symbol", "price", "quantity",
            "buyer_id", "seller_id", "aggressor", "fee", "timestamp",
        ]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in self.exchange.trade_log:
                writer.writerow({
                    "trade_id": t.trade_id,
                    "symbol": t.symbol,
                    "price": t.price,
                    "quantity": t.quantity,
                    "buyer_id": t.buyer_id,
                    "seller_id": t.seller_id,
                    "aggressor": t.aggressor,
                    "fee": t.fee,
                    "timestamp": t.timestamp,
                })
        print(f"  Trade log → {path}  ({len(self.exchange.trade_log)} rows)")


# ─────────────────────────────────────────────────────────────────────────────
# SimSession
# ─────────────────────────────────────────────────────────────────────────────

class SimSession:
    """Orchestrates a full in-process trading session simulation.

    Usage:
        result = SimSession().run(
            n_ticks=500,
            strategies=[("my_bot", my_signal_fn)],
            symbols=["AAPL", "TSLA"],
        )
    """

    def run(
        self,
        n_ticks: int = 1000,
        strategies: list[tuple[str, SignalFn]] | None = None,
        symbols: list[str] | None = None,
        fee_rate: float = 0.001,
        broker_spread: float = 0.30,
        broker_quote_size: int = 5,
        seed: int | None = None,
        verbose: bool = True,
    ) -> SessionResult:
        """Run the simulation and return a SessionResult.

        Parameters
        ----------
        n_ticks:
            Number of one-second ticks to simulate.
        strategies:
            List of (team_id, signal_fn) tuples.  The signal_fn must match
            the plugin registry signature:
                signal_fn(symbol, prices, history, book, portfolio) -> Signal | None
        symbols:
            List of registered security symbols to include.  Defaults to
            ["AAPL", "TSLA", "BTC"].
        fee_rate:
            Transaction fee (fraction of notional).  Default 0.001 (0.1%).
        broker_spread:
            Fixed $ bid-ask spread posted by the simulated broker.
        broker_quote_size:
            Shares per side posted by the broker.
        seed:
            Random seed for reproducibility.  None = non-deterministic.
        verbose:
            Print a progress line every 10% of ticks.
        """
        if seed is not None:
            random.seed(seed)

        if symbols is None:
            symbols = ["AAPL", "TSLA", "BTC"]

        if strategies is None:
            strategies = []

        # ── Build components ─────────────────────────────────────────
        exchange = SimulatedExchange(symbols=symbols, fee_rate=fee_rate)
        broker = SimulatedBroker(
            exchange, team_id="sim_broker",
            spread=broker_spread, quote_size=broker_quote_size,
        )
        traders = [
            SimulatedTrader(exchange, team_id=tid, signal_fn=fn, symbols=symbols)
            for tid, fn in strategies
        ]

        # ── Price history collector ───────────────────────────────────
        price_history: dict[str, list[float]] = {sym: [] for sym in exchange.symbols}

        # Snapshot initial prices
        for sym in exchange.symbols:
            price_history[sym].append(exchange.ref_prices[sym])

        # ── Main simulation loop ──────────────────────────────────────
        checkpoint = max(1, n_ticks // 10)
        t0 = time.perf_counter()

        for tick_n in range(n_ticks):
            # 1. Advance GBM prices.
            exchange.advance_tick()

            # 2. Record prices.
            for sym in exchange.symbols:
                price_history[sym].append(exchange.ref_prices[sym])

            # 3. Broker requotes all symbols.
            broker.tick()

            # 4. Each trader generates and places a signal.
            for trader in traders:
                trader.tick()

            if verbose and (tick_n + 1) % checkpoint == 0:
                elapsed = time.perf_counter() - t0
                pct = (tick_n + 1) / n_ticks * 100
                print(f"  tick {tick_n + 1:>5}/{n_ticks}  ({pct:.0f}%)  "
                      f"trades={len(exchange.trade_log)}  "
                      f"elapsed={elapsed:.1f}s")

        elapsed = time.perf_counter() - t0
        if verbose:
            print(f"  Simulation complete in {elapsed:.2f}s")

        return SessionResult(
            n_ticks=n_ticks,
            symbols=exchange.symbols,
            price_history=price_history,
            leaderboard=exchange.leaderboard(),
            traders=traders,
            broker=broker,
            exchange=exchange,
        )


# ─────────────────────────────────────────────────────────────────────────────
# __main__ — demonstration run
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    # Import example strategies directly (public aliases added in examples.py).
    from plugins.strategies.examples import (
        momentum_signal,
        mean_reversion_signal,
        do_nothing_signal,
    )

    # SYNTH is a pure sine wave (period=3600 ticks, amplitude=±$10 around $100).
    # It reliably triggers all strategies within a few hundred ticks, making it
    # the ideal security for demo runs and integration tests.
    # AAPL is included so pairs_trader and book_imbalance strategies have two symbols.
    print("\nRunning AlgoArena simulation — 3 600 ticks, 3 traders, SYNTH + AAPL…")
    print("(3 600 ticks = one full sine cycle on SYNTH: prices swing $90→$110→$90)\n")

    result = SimSession().run(
        n_ticks=3_600,
        strategies=[
            ("momentum_bot",    momentum_signal),
            ("mean_rev_bot",    mean_reversion_signal),
            ("do_nothing_bot",  do_nothing_signal),
        ],
        symbols=["SYNTH", "AAPL"],
        seed=42,
    )

    result.print_summary()
    result.plot_pnl()
