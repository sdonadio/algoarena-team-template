#!/usr/bin/env python
"""
scripts/backtest.py — Replay a recorded session through a student strategy.

Feeds a session recording (sessions/session_*.jsonl) into a strategy class
offline: no exchange, no network, no other participants. Useful for tuning a
signal between live sessions and for grading strategy work.

Usage:
    python scripts/backtest.py sessions/session_20260820_141530.jsonl \\
        --module trader.trader
    python scripts/backtest.py <file> --module students.team_alpha.trader \\
        --class MomentumStrategy --cash 100000
    python scripts/backtest.py <file> --module trader.trader --csv equity.csv
    python scripts/backtest.py --latest --module trader.trader --every 5

MODEL — read this before trusting a number
==========================================

Strategy discovery
    --module is imported, then: the first class *defined in that module* that
    subclasses trader.trader.Strategy is used. Use --class to name one
    explicitly. A class that only exposes signal(market, portfolio) returning
    (symbol, side, qty, price) is wrapped in a best-effort adapter; anything
    else exits with a list of candidate classes.

Market state
    Every book_snapshot in the recording is pushed into a trader.trader
    MarketData via update_book(), and every trade_execution via update_trade(),
    in recorded order. The strategy therefore sees exactly the tape the live
    bots saw, including the mid-price history it builds from snapshots.

Tick definition
    A strategy tick fires after every book_snapshot is ingested (one tick per
    book update of any symbol) — the offline analogue of the live bot's
    TICK_INTERVAL_SEC loop. --every N fires only on every Nth snapshot.

Fill model (deliberately simple and slightly pessimistic)
    - A buy fills at the current best ask, a sell at the current best bid.
    - Fill quantity is capped by the size displayed at that top level, so a
      signal larger than the touch is partially filled. The rest is dropped
      (no resting orders, no queue position, no order book impact).
    - No fill at all if that side of the book is empty, or if a buy's full
      cost (notional + fee) exceeds available cash.
    - Every fill is a taker fill and pays TAKER_FEE (0.15% of notional by
      default, matching exchange TAKER_FEE_RATE). Maker rebates are not
      reachable in this model.
    - Sells may open short positions; margin, borrow carry and liquidation
      are ignored. Positions are marked to the mid.
    - The backtest cannot model market impact or the reaction of the other
      participants: it replays a tape that your fills did not influence.

Metrics
    Net worth = cash + Σ position × mid, recorded at every tick (the "marks").
    Sharpe-like = mean/σ of per-mark returns × √n (σ over n-1), the same
    formula family the dashboard uses. Max drawdown = worst peak-to-trough
    drop of the mark series.
"""

from __future__ import annotations

import argparse
import csv as csvmod
import glob
import importlib
import json
import pathlib
import sys
import time
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if (ROOT / "engine").is_dir():     # student-template layout
    sys.path.insert(0, str(ROOT / "engine"))

from pydantic import ValidationError                              # noqa: E402
from rich.console import Console                                  # noqa: E402
from rich.table import Table                                      # noqa: E402

from shared.messages import (                                     # noqa: E402
    BookSnapshot,
    Signal,
    TradeExecution,
    parse_message,
)
from trader.trader import MarketData, Portfolio, Strategy         # noqa: E402

_console = Console()

TAKER_FEE = 0.0015          # exchange TAKER_FEE_RATE default (0.15% of notional)
_SPARK = "▁▂▃▄▅▆▇█"
_ABORT_AFTER = 25           # consecutive strategy errors from tick 1 → give up


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio — a Portfolio-compatible ledger that marks to the replayed book
# ─────────────────────────────────────────────────────────────────────────────

class BacktestPortfolio(Portfolio):
    """Local ledger with the same interface the live Portfolio exposes.

    The live Portfolio is a mirror of exchange state; this one *is* the state.
    Average-cost accounting matches exchange/server.py so realized P&L is
    comparable with a live session.
    """

    def __init__(self, cash: float, team_id: str = "backtest") -> None:
        super().__init__()
        self.team_id = team_id
        self.cash = cash
        self.starting_cash = cash
        self.positions = {}
        self.avg_cost: dict[str, float] = {}
        self.realized_pnl = 0.0
        self.total_fees_paid = 0.0
        self._unrealized_pnl = 0.0
        self._net_worth = cash

    # -- fills --------------------------------------------------------------

    def apply_buy(self, symbol: str, price: float, qty: int, fee: float) -> None:
        """Debit cash, grow the position, update average cost."""
        old_qty = max(0, self.positions.get(symbol, 0))
        old_cost = self.avg_cost.get(symbol, 0.0) * old_qty
        new_qty = old_qty + qty
        self.cash -= price * qty + fee
        self.total_fees_paid += fee
        self.positions[symbol] = self.positions.get(symbol, 0) + qty
        self.avg_cost[symbol] = (old_cost + price * qty) / new_qty if new_qty else price

    def apply_sell(self, symbol: str, price: float, qty: int, fee: float) -> None:
        """Credit cash, realise P&L against average cost, shrink the position."""
        avg = self.avg_cost.get(symbol, price)
        self.cash += price * qty - fee
        self.total_fees_paid += fee
        self.realized_pnl += (price - avg) * qty
        self.positions[symbol] = self.positions.get(symbol, 0) - qty

    # -- marks --------------------------------------------------------------

    def _mark(self, market: MarketData, symbol: str) -> float:
        return market.mid_price(symbol) or self.avg_cost.get(symbol, 0.0)

    def unrealized_pnl(self, market: MarketData) -> float:
        """Mark-to-mid unrealised P&L across all open positions."""
        total = 0.0
        for sym, qty in self.positions.items():
            if qty:
                mark = self._mark(market, sym)
                total += (mark - self.avg_cost.get(sym, mark)) * qty
        return total

    def net_worth(self, market: MarketData) -> float:
        """Cash plus mark-to-mid value of every open position."""
        mv = sum(qty * self._mark(market, sym)
                 for sym, qty in self.positions.items() if qty)
        return self.cash + mv

    def refresh(self, market: MarketData) -> float:
        """Recompute the cached fields the live Portfolio serves and return NW."""
        self._unrealized_pnl = self.unrealized_pnl(market)
        self._net_worth = self.net_worth(market)
        return self._net_worth


# ─────────────────────────────────────────────────────────────────────────────
# Strategy resolution
# ─────────────────────────────────────────────────────────────────────────────

class StrategyError(RuntimeError):
    """Raised when no usable strategy class can be found or instantiated."""


class _TupleSignalAdapter:
    """Adapt a class that exposes signal() to the generate_signal() interface.

    Accepts a (symbol, side, quantity, price) tuple, a Signal, or None. This is
    best-effort: a strategy written against its own Market/Portfolio classes may
    still fail on attributes trader.trader.MarketData does not provide.
    """

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def generate_signal(
        self, market: MarketData, portfolio: Portfolio
    ) -> Signal | None:
        out = self.inner.signal(market, portfolio)          # type: ignore[attr-defined]
        if out is None or isinstance(out, Signal):
            return out
        symbol, side, qty, price = out
        return Signal(symbol=symbol, side=side, quantity=int(qty), price=float(price))


def _module_classes(module: object) -> list[tuple[str, type]]:
    """Classes defined in this module, in source (definition) order."""
    name = getattr(module, "__name__", "")
    return [
        (attr, obj) for attr, obj in vars(module).items()
        if isinstance(obj, type) and obj.__module__ == name
    ]


def _candidate_report(module: object) -> str:
    lines = []
    for attr, obj in _module_classes(module):
        marks = []
        if callable(getattr(obj, "generate_signal", None)):
            marks.append("has generate_signal(market, portfolio) ✓")
        elif callable(getattr(obj, "signal", None)):
            marks.append("has signal(market, portfolio) — adapter, may not fit")
        lines.append(f"    {attr}" + (f"   [{'; '.join(marks)}]" if marks else ""))
    return "\n".join(lines) or "    (no classes defined in this module)"


def resolve_strategy(module_path: str, class_name: str | None) -> tuple[object, str]:
    """Import module_path and return (strategy_instance, class_name).

    Raises StrategyError with an actionable message when nothing usable exists.
    """
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:                       # ImportError or import-time crash
        raise StrategyError(f"could not import {module_path!r}: {exc}") from exc

    if class_name:
        cls = getattr(module, class_name, None)
        if not isinstance(cls, type):
            raise StrategyError(
                f"{module_path} has no class named {class_name!r}. Candidates:\n"
                f"{_candidate_report(module)}"
            )
    else:
        # Strategy itself counts when the module *defines* it, which is how the
        # trader/trader.py template is backtested; a student module that merely
        # imports Strategy is filtered out by the __module__ check.
        subclasses = [
            obj for _, obj in _module_classes(module) if issubclass(obj, Strategy)
        ]
        if not subclasses:
            raise StrategyError(
                f"no subclass of trader.trader.Strategy is defined in "
                f"{module_path}. Re-run with --class <name>. Candidates:\n"
                f"{_candidate_report(module)}"
            )
        cls = subclasses[0]

    try:
        instance: object = cls()
    except TypeError as exc:
        raise StrategyError(
            f"{cls.__name__} could not be instantiated with no arguments: {exc}"
        ) from exc

    if callable(getattr(instance, "generate_signal", None)):
        return instance, cls.__name__
    if callable(getattr(instance, "signal", None)):
        _console.print(
            f"[yellow]{cls.__name__} has no generate_signal() — wrapping its "
            f"signal() method (best effort)[/yellow]"
        )
        return _TupleSignalAdapter(instance), cls.__name__
    raise StrategyError(
        f"{cls.__name__} exposes neither generate_signal(market, portfolio) nor "
        f"signal(market, portfolio), so it cannot be backtested. Candidates:\n"
        f"{_candidate_report(module)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Backtest engine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BTFill:
    ts: float
    symbol: str
    side: str
    price: float
    quantity: int
    fee: float


@dataclass
class BacktestResult:
    recording: pathlib.Path
    strategy_name: str
    starting_cash: float
    final_net_worth: float
    equity: list[float] = field(default_factory=list)
    marks_ts: list[float] = field(default_factory=list)
    fills: list[BTFill] = field(default_factory=list)
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    snapshots: int = 0
    trades_seen: int = 0
    ticks: int = 0
    skipped_lines: int = 0
    signals: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    strategy_errors: int = 0
    first_error: str = ""
    aborted: bool = False       # strategy never once ran cleanly

    # -- derived metrics ----------------------------------------------------

    @property
    def total_return(self) -> float:
        return (self.final_net_worth / self.starting_cash - 1.0) if self.starting_cash else 0.0

    @property
    def returns(self) -> list[float]:
        return [
            self.equity[i] / self.equity[i - 1] - 1.0
            for i in range(1, len(self.equity))
            if self.equity[i - 1] > 0
        ]

    @property
    def sharpe(self) -> float | None:
        """mean/σ of per-mark returns × √n — the dashboard's formula family."""
        rets = self.returns
        n = len(rets)
        if n < 4:
            return None
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        sd = var ** 0.5
        return mean / sd * (n ** 0.5) if sd > 1e-12 else 0.0

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough drop of the mark series, as a fraction."""
        peak, worst = 0.0, 0.0
        for v in self.equity:
            peak = max(peak, v)
            if peak > 0:
                worst = max(worst, (peak - v) / peak)
        return worst


def _top(levels: list[list[float]]) -> tuple[float | None, int]:
    """(price, size) of the best level of one book side."""
    if not levels:
        return None, 0
    price, qty = levels[0][0], levels[0][1]
    return price, int(qty)


class Backtester:
    """Replays one recording through one strategy instance."""

    def __init__(
        self,
        strategy: object,
        cash: float,
        fee_rate: float = TAKER_FEE,
        every: int = 1,
    ) -> None:
        self.strategy = strategy
        self.market = MarketData()
        self.portfolio = BacktestPortfolio(cash)
        self.fee_rate = fee_rate
        self.every = max(1, every)
        # symbol → (best_bid, bid_size, best_ask, ask_size) from the last snapshot
        self._touch: dict[str, tuple[float | None, int, float | None, int]] = {}

    # -- fill model ---------------------------------------------------------

    def _try_fill(self, signal: Signal, ts: float) -> tuple[BTFill | None, str]:
        """Apply the fill model to one signal. Returns (fill or None, reason)."""
        touch = self._touch.get(signal.symbol)
        if not touch:
            return None, "no book"
        bid, bid_sz, ask, ask_sz = touch

        if signal.side == "buy":
            price, available = ask, ask_sz
        else:
            price, available = bid, bid_sz
        if price is None or available <= 0:
            return None, "empty book side"

        qty = min(int(signal.quantity), available)
        if qty <= 0:
            return None, "zero quantity"

        notional = price * qty
        fee = notional * self.fee_rate

        if signal.side == "buy":
            if self.portfolio.cash < notional + fee:
                return None, "insufficient cash"
            self.portfolio.apply_buy(signal.symbol, price, qty, fee)
        else:
            self.portfolio.apply_sell(signal.symbol, price, qty, fee)

        return BTFill(ts=ts, symbol=signal.symbol, side=signal.side,
                      price=price, quantity=qty, fee=fee), "filled"

    # -- main loop ----------------------------------------------------------

    def run(
        self,
        path: pathlib.Path,
        strategy_name: str,
        speed: str = "report-only",
    ) -> BacktestResult:
        """Stream the recording, tick the strategy, and collect results."""
        res = BacktestResult(
            recording=path,
            strategy_name=strategy_name,
            starting_cash=self.portfolio.starting_cash,
            final_net_worth=self.portfolio.starting_cash,
        )
        paced = speed != "report-only"
        multiplier = 1.0
        if paced:
            try:
                multiplier = max(0.1, float(speed))
            except ValueError:
                paced = False
        prev_ts: float | None = None
        snap_count = 0
        clean_calls = 0          # ticks on which the strategy returned normally

        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec["ts"])
                    msg = parse_message(rec["msg"])
                except (ValueError, TypeError, KeyError, AttributeError,
                        ValidationError):
                    res.skipped_lines += 1
                    continue

                if paced and prev_ts is not None and ts > prev_ts:
                    time.sleep(min(2.0, (ts - prev_ts) / multiplier))
                prev_ts = ts

                if isinstance(msg, TradeExecution):
                    res.trades_seen += 1
                    self.market.update_trade(msg)
                    continue

                if not isinstance(msg, BookSnapshot):
                    continue        # leaderboards, session events: not inputs here

                # ---- market state ----
                res.snapshots += 1
                snap_count += 1
                self.market.update_book(msg)
                bid, bid_sz = _top(msg.bids)
                ask, ask_sz = _top(msg.asks)
                self._touch[msg.symbol] = (bid, bid_sz, ask, ask_sz)

                if snap_count % self.every:
                    continue

                # ---- strategy tick ----
                res.ticks += 1
                try:
                    signal = self.strategy.generate_signal(   # type: ignore[attr-defined]
                        self.market, self.portfolio
                    )
                except Exception as exc:                      # never crash the run
                    res.strategy_errors += 1
                    if not res.first_error:
                        res.first_error = f"{type(exc).__name__}: {exc}"
                    signal = None
                    # A strategy that has never once returned cleanly is not
                    # compatible with this interface — stop instead of printing
                    # a meaningless flat report over thousands of ticks.
                    if not clean_calls and res.strategy_errors >= _ABORT_AFTER:
                        res.aborted = True
                        break
                else:
                    clean_calls += 1

                if signal is not None:
                    res.signals += 1
                    fill, reason = self._try_fill(signal, ts)
                    if fill:
                        res.fills.append(fill)
                    else:
                        res.rejected[reason] = res.rejected.get(reason, 0) + 1

                # ---- mark ----
                res.equity.append(self.portfolio.refresh(self.market))
                res.marks_ts.append(ts)

        res.final_net_worth = self.portfolio.refresh(self.market)
        res.fees_paid = self.portfolio.total_fees_paid
        res.realized_pnl = self.portfolio.realized_pnl
        res.unrealized_pnl = self.portfolio._unrealized_pnl
        res.positions = {s: q for s, q in self.portfolio.positions.items() if q}
        return res


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def _positions(positions: dict[str, int]) -> str:
    if not positions:
        return "flat"
    return ", ".join(f"{sym} {qty:+d}" for sym, qty in sorted(positions.items()))


def sparkline(values: list[float], width: int = 60) -> str:
    """Unicode block sparkline of an equity curve, downsampled to `width`."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[min(len(values) - 1, int(i * step))] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return _SPARK[len(_SPARK) // 2] * len(values)
    span = len(_SPARK) - 1
    return "".join(_SPARK[int((v - lo) / (hi - lo) * span)] for v in values)


def print_report(res: BacktestResult) -> None:
    """Print the full backtest report to the terminal."""
    buys = [f for f in res.fills if f.side == "buy"]
    sells = [f for f in res.fills if f.side == "sell"]
    pnl = res.final_net_worth - res.starting_cash
    pnl_c = "green" if pnl >= 0 else "red"

    table = Table(title=f"Backtest — {res.strategy_name} on {res.recording.name}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Starting cash", f"${res.starting_cash:,.2f}")
    table.add_row("Final net worth", f"${res.final_net_worth:,.2f}")
    table.add_row("P&L", f"[{pnl_c}]{pnl:+,.2f}[/{pnl_c}]")
    table.add_row("Total return", f"[{pnl_c}]{res.total_return * 100:+.2f}%[/{pnl_c}]")
    table.add_row("Realized / Unrealized",
                  f"{res.realized_pnl:+,.2f} / {res.unrealized_pnl:+,.2f}")
    table.add_row("Trades (fills)", f"{len(res.fills)}  ({len(buys)} buy / {len(sells)} sell)")
    table.add_row("Shares traded", f"{sum(f.quantity for f in res.fills):,}")
    table.add_row("Fees paid", f"${res.fees_paid:,.2f}")
    table.add_row("Max drawdown", f"{res.max_drawdown * 100:.2f}%")
    table.add_row("Sharpe-like",
                  "—" if res.sharpe is None else f"{res.sharpe:+.2f}")
    table.add_row("Ticks / snapshots / tape trades",
                  f"{res.ticks:,} / {res.snapshots:,} / {res.trades_seen:,}")
    table.add_row("Signals emitted", f"{res.signals:,}")
    table.add_row("Open positions", _positions(res.positions))
    _console.print(table)

    if res.equity:
        curve_c = "green" if res.equity[-1] >= res.equity[0] else "red"
        _console.print(f"  equity  [{curve_c}]{sparkline(res.equity)}[/{curve_c}]")
        _console.print(f"  ${min(res.equity):,.0f} … ${max(res.equity):,.0f} "
                       f"over {len(res.equity):,} marks")

    if res.rejected:
        detail = "  ".join(f"{k}: {v}" for k, v in sorted(res.rejected.items()))
        _console.print(f"[yellow]  signals not filled — {detail}[/yellow]")
    if res.skipped_lines:
        _console.print(f"[yellow]  {res.skipped_lines} malformed line(s) skipped[/yellow]")
    if res.strategy_errors:
        _console.print(
            f"[red]  strategy raised on {res.strategy_errors}/{res.ticks} ticks — "
            f"first error: {res.first_error}[/red]"
        )


def write_csv(res: BacktestResult, path: pathlib.Path) -> None:
    """Dump the equity curve for plotting elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csvmod.writer(fh)
        w.writerow(["mark", "ts", "net_worth"])
        for i, (ts, nw) in enumerate(zip(res.marks_ts, res.equity)):
            w.writerow([i, f"{ts:.3f}", f"{nw:.4f}"])


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Replay a recorded AlgoArena session through a strategy"
    )
    ap.add_argument("file", nargs="?", help="sessions/session_*.jsonl")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest recording in sessions/")
    ap.add_argument("--module", required=True,
                    help="import path of the strategy module, e.g. trader.trader")
    ap.add_argument("--class", dest="class_name", default=None,
                    help="strategy class name (default: first Strategy subclass)")
    ap.add_argument("--cash", type=float, default=100_000.0,
                    help="starting cash (default: 100000)")
    ap.add_argument("--fee", type=float, default=TAKER_FEE,
                    help=f"taker fee rate on notional (default: {TAKER_FEE})")
    ap.add_argument("--every", type=int, default=1,
                    help="tick the strategy every Nth book snapshot (default: 1)")
    ap.add_argument("--speed", default="report-only",
                    help="'report-only' (as fast as possible) or a playback "
                         "multiplier such as 10 to pace against recorded time")
    ap.add_argument("--csv", default=None, help="write the equity curve here")
    args = ap.parse_args(argv)

    path = args.file
    if args.latest or not path:
        candidates = sorted(glob.glob(str(ROOT / "sessions" / "session_*.jsonl")))
        if not candidates:
            print("No recordings found in sessions/ — run a session first",
                  file=sys.stderr)
            return 2
        path = candidates[-1]
        print(f"  Using latest recording: {path}")

    recording = pathlib.Path(path)
    if not recording.exists():
        print(f"No such recording: {recording}", file=sys.stderr)
        return 2

    try:
        strategy, name = resolve_strategy(args.module, args.class_name)
    except StrategyError as exc:
        print(f"backtest: {exc}", file=sys.stderr)
        return 2

    bt = Backtester(strategy, cash=args.cash, fee_rate=args.fee, every=args.every)
    res = bt.run(recording, name, speed=args.speed)

    if not res.snapshots:
        print(f"backtest: no book snapshots in {recording} — nothing to replay",
              file=sys.stderr)
        return 1

    if res.aborted:
        print(f"backtest: {name} raised on all of its first {res.strategy_errors} "
              f"ticks and never ran cleanly — it does not implement the "
              f"trader.trader.Strategy interface.\n"
              f"          first error: {res.first_error}", file=sys.stderr)
        return 2

    print_report(res)

    if args.csv:
        write_csv(res, pathlib.Path(args.csv))
        _console.print(f"  equity curve → [bold]{args.csv}[/bold]")

    if res.ticks and res.strategy_errors == res.ticks:
        print(f"backtest: the strategy raised on every tick — {res.first_error}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
