"""
trader/shock_trader.py — Shock-reactive trading bot for Team Quant.

Strategy overview:
  - Between shocks: slowly accumulates a balanced long book across all symbols
    so there is always inventory available to sell on bearish shocks.
  - On a SHOCK SessionEvent: immediately queues a burst of pre-computed signals
    calibrated to the exact shock type. One signal executes per tick so the bot
    reacts faster than competitors who re-compute each tick.

Shock playbook:
  flash_crash      → BUY all (cheapest first, deeper % recovery = more profit)
  risk_on_rally    → BUY high-beta (NVDA, TSLA, AMD, NFLX, META) first
  fed_rate_hike    → SELL rate-sensitive growth stocks (TSLA, NVDA, META, …)
  fed_rate_cut     → BUY rate-sensitive growth stocks
  earnings_beat    → BUY the affected symbol on momentum
  earnings_miss    → SELL affected; BUY the rest (capital rotation)
  sector_rotate    → BUY all equities (all symbols are equities here)
  geo_crisis       → SELL everything (risk assets fall)
  vol_spike        → BUY symbols that moved up, SELL symbols that moved down
  liquidity_crunch → BUY quickly before broker re-quotes wider spreads

Run:
    TEAM_ID=quant_trader_1 python -m trader.shock_trader
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

import websockets
import websockets.exceptions
from rich.console import Console

import trader.config as config
from shared.messages import (
    BookSnapshot,
    ErrorMsg,
    Handshake,
    Leaderboard,
    OrderAck,
    PlaceOrder,
    PortfolioUpdate,
    SessionEvent,
    Signal,
    TradeExecution,
    parse_message,
)
from trader.trader import MarketData, Portfolio

logger = logging.getLogger(__name__)
_console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# ShockReactiveStrategy
# ─────────────────────────────────────────────────────────────────────────────

class ShockReactiveStrategy:
    """Dispatches to a shock-specific signal generator, queues results."""

    # Symbols ranked by implied beta / rate sensitivity
    HIGH_BETA      = ["NVDA", "TSLA", "AMD", "NFLX", "META"]
    RATE_SENSITIVE = ["TSLA", "NVDA", "META", "AMZN", "GOOGL", "AMD"]

    # Between shocks: target shares held per symbol
    IDLE_TARGET = 20
    # Max dollar spend per symbol in a shock burst
    MAX_SPEND_PER_SYM = 8_000.0
    # Cap on queued signals per shock
    MAX_QUEUE = 14
    # Max shares to trim per idle tick (avoids flooding the book)
    TRIM_BATCH = 25

    def __init__(self) -> None:
        self._queue: deque[Signal] = deque()
        self._idle_symbols: list[str] = []
        self._idle_idx: int = 0

    # ── Shock dispatch ────────────────────────────────────────────────────────

    def on_shock(
        self,
        shock_id: str,
        affected: list[str],
        shock_prices: dict[str, float],
        market: MarketData,
        portfolio: Portfolio,
    ) -> None:
        """Called the moment a SHOCK SessionEvent arrives."""
        self._queue.clear()
        handlers: dict[str, Any] = {
            "flash_crash":      self._q_flash_crash,
            "risk_on_rally":    self._q_risk_on_rally,
            "fed_rate_hike":    self._q_fed_hike,
            "fed_rate_cut":     self._q_fed_cut,
            "earnings_beat":    self._q_earnings_beat,
            "earnings_miss":    self._q_earnings_miss,
            "sector_rotate":    self._q_sector_rotate,
            "geo_crisis":       self._q_geo_crisis,
            "vol_spike":        self._q_vol_spike,
            "liquidity_crunch": self._q_liquidity_crunch,
        }
        fn = handlers.get(shock_id)
        if fn is None:
            logger.warning("No handler for shock '%s'", shock_id)
            return
        try:
            signals = fn(affected, shock_prices, market, portfolio)
            self._queue.extend(signals[: self.MAX_QUEUE])
            logger.info("Shock '%s' → queued %d signals", shock_id, len(self._queue))
        except Exception:
            logger.exception("ShockStrategy raised for '%s'", shock_id)

    def generate_signal(
        self, market: MarketData, portfolio: Portfolio
    ) -> Signal | None:
        if self._queue:
            return self._queue.popleft()
        return self._idle_signal(market, portfolio)

    # ── Idle accumulation ─────────────────────────────────────────────────────

    def _idle_signal(
        self, market: MarketData, portfolio: Portfolio
    ) -> Signal | None:
        """Trim excess positions back to IDLE_TARGET, then build up to target.

        Trimming always fires when held > IDLE_TARGET — no price condition.
        This ensures every shock buy eventually converts to realized P&L
        regardless of whether the position is in profit or at a loss.
        """
        syms = market.symbols()
        if not syms:
            return None
        if set(syms) != set(self._idle_symbols):
            self._idle_symbols = sorted(syms)
            self._idle_idx = 0

        # Trim pass: sell excess shares from the last shock buy.
        # Cycle through symbols so one large position doesn't block others.
        for sym in self._idle_symbols:
            held = portfolio.positions.get(sym, 0)
            if held <= self.IDLE_TARGET:
                continue
            bid = market.best_bid(sym)
            if bid:
                sell_qty = min(held - self.IDLE_TARGET, self.TRIM_BATCH)
                return Signal(symbol=sym, side="sell", quantity=sell_qty,
                              price=round(bid * 0.999, 4), confidence=0.7)

        # Accumulation pass: buy one share of the next under-target symbol.
        for _ in range(len(self._idle_symbols)):
            sym = self._idle_symbols[self._idle_idx % len(self._idle_symbols)]
            self._idle_idx += 1
            if portfolio.positions.get(sym, 0) < self.IDLE_TARGET:
                ask = market.best_ask(sym)
                if ask and portfolio.can_buy(sym, 1, ask * 1.001):
                    return Signal(
                        symbol=sym, side="buy", quantity=1,
                        price=round(ask * 1.002, 4), confidence=0.3,
                    )
        return None

    # ── Signal-building helpers ───────────────────────────────────────────────

    def _buy_symbols(
        self,
        symbols: list[str],
        market: MarketData,
        portfolio: Portfolio,
        premium: float = 1.005,
        max_qty: int = 20,
    ) -> list[Signal]:
        """Build aggressive BUY signals for the given symbol list."""
        signals = []
        for sym in symbols:
            ask = market.best_ask(sym)
            if not ask:
                continue
            qty = max(1, min(max_qty, int(self.MAX_SPEND_PER_SYM / ask)))
            px = round(ask * premium, 4)
            if portfolio.can_buy(sym, qty, px):
                signals.append(
                    Signal(symbol=sym, side="buy", quantity=qty,
                           price=px, confidence=0.95)
                )
        return signals

    def _sell_held(
        self,
        symbols: list[str],
        portfolio: Portfolio,
        market: MarketData,
        discount: float = 0.995,
    ) -> list[Signal]:
        """Sell all held shares by hitting the bid with a small discount."""
        signals = []
        for sym in symbols:
            held = portfolio.positions.get(sym, 0)
            if held <= 0:
                continue
            bid = market.best_bid(sym)
            if not bid:
                continue
            signals.append(
                Signal(symbol=sym, side="sell", quantity=held,
                       price=round(bid * discount, 4), confidence=0.95)
            )
        return signals

    # ── Per-shock handlers ────────────────────────────────────────────────────

    def _q_flash_crash(self, affected, shock_prices, market, portfolio):
        # All assets crashed — buy everything, cheapest first for more shares.
        syms = sorted(market.symbols(), key=lambda s: market.mid_price(s) or 1e9)
        return self._buy_symbols(syms, market, portfolio, premium=1.01, max_qty=60)

    def _q_risk_on_rally(self, affected, shock_prices, market, portfolio):
        # Risk-on surge — high-beta names amplify the move the most.
        priority = [s for s in self.HIGH_BETA if s in market.symbols()]
        rest = [s for s in market.symbols() if s not in priority]
        return self._buy_symbols(priority + rest, market, portfolio,
                                 premium=1.005, max_qty=40)

    def _q_fed_hike(self, affected, shock_prices, market, portfolio):
        # Rates up — sell rate-sensitive growth stocks first.
        priority = [s for s in self.RATE_SENSITIVE if s in market.symbols()]
        rest = [s for s in market.symbols() if s not in priority]
        return self._sell_held(priority + rest, portfolio, market, discount=0.993)

    def _q_fed_cut(self, affected, shock_prices, market, portfolio):
        # Rates down — rate-sensitive names get the biggest boost.
        priority = [s for s in self.RATE_SENSITIVE if s in market.symbols()]
        rest = [s for s in market.symbols() if s not in priority]
        return self._buy_symbols(priority + rest, market, portfolio,
                                 premium=1.005, max_qty=40)

    def _q_earnings_beat(self, affected, shock_prices, market, portfolio):
        # One stock beat — buy it aggressively; price usually keeps running.
        return self._buy_symbols(
            affected, market, portfolio, premium=1.01, max_qty=50
        )

    def _q_earnings_miss(self, affected, shock_prices, market, portfolio):
        # Sell the loser; buy the rest (capital rotates out of the miss).
        signals = self._sell_held(affected, portfolio, market, discount=0.99)
        rest = [s for s in market.symbols() if s not in affected]
        signals += self._buy_symbols(rest, market, portfolio,
                                     premium=1.003, max_qty=20)
        return signals

    def _q_sector_rotate(self, affected, shock_prices, market, portfolio):
        # Rotation from crypto into equities — all symbols are equities, so buy all.
        return self._buy_symbols(market.symbols(), market, portfolio,
                                 premium=1.005, max_qty=30)

    def _q_geo_crisis(self, affected, shock_prices, market, portfolio):
        # Geo crisis tanks risk assets — flatten everything.
        return self._sell_held(market.symbols(), portfolio, market, discount=0.99)

    def _q_vol_spike(self, affected, shock_prices, market, portfolio):
        # Each symbol moved independently ±4-8%.
        # shock_prices contains POST-shock levels; market still has PRE-shock mid.
        buy_syms, sell_syms = [], []
        for sym in market.symbols():
            new_px = shock_prices.get(sym)
            old_px = market.mid_price(sym)
            if new_px is None or not old_px or old_px == 0:
                continue
            chg = (new_px - old_px) / old_px
            if chg > 0.02:
                buy_syms.append((sym, chg))
            elif chg < -0.02:
                sell_syms.append((sym, chg))

        buy_syms.sort(key=lambda x: x[1], reverse=True)
        sell_syms.sort(key=lambda x: x[1])

        signals  = self._buy_symbols([s for s, _ in buy_syms], market, portfolio,
                                     premium=1.008, max_qty=40)
        signals += self._sell_held([s for s, _ in sell_syms], portfolio, market,
                                   discount=0.992)
        return signals

    def _q_liquidity_crunch(self, affected, shock_prices, market, portfolio):
        # Spreads widen but prices unchanged — buy quickly before re-quotes arrive.
        syms = sorted(market.symbols(), key=lambda s: market.mid_price(s) or 1e9)
        return self._buy_symbols(syms, market, portfolio, premium=1.002, max_qty=25)


# ─────────────────────────────────────────────────────────────────────────────
# ShockTraderBot
# ─────────────────────────────────────────────────────────────────────────────

class ShockTraderBot:
    """WebSocket trading bot that wires ShockReactiveStrategy to the exchange."""

    def __init__(self) -> None:
        self.market    = MarketData()
        self.portfolio = Portfolio()
        self.strategy  = ShockReactiveStrategy()
        self._ws: Any  = None
        self._session_open = False

    async def connect(self) -> None:
        self._ws = await websockets.connect(config.EXCHANGE_URL)
        await self._ws.send(
            Handshake(team_id=config.TEAM_ID, role="trader", level=5,
                      token=config.ARENA_TOKEN).model_dump_json()
        )
        logger.info("ShockTrader connected as %s → %s", config.TEAM_ID, config.EXCHANGE_URL)

    async def _send(self, msg: Any) -> None:
        try:
            await self._ws.send(msg.model_dump_json())
        except websockets.exceptions.ConnectionClosed:
            pass

    # ── Message listener ──────────────────────────────────────────────────────

    async def listen(self) -> None:
        async for raw in self._ws:
            try:
                msg = parse_message(json.loads(raw))
            except (KeyError, ValueError) as exc:
                logger.warning("Unrecognised message: %s", exc)
                continue

            if isinstance(msg, BookSnapshot):
                self.market.update_book(msg)
            elif isinstance(msg, TradeExecution):
                self.market.update_trade(msg)
                if msg.buyer_id == config.TEAM_ID:
                    logger.info("Fill BUY  %d %s @ %.2f", msg.quantity, msg.symbol, msg.price)
                elif msg.seller_id == config.TEAM_ID:
                    logger.info("Fill SELL %d %s @ %.2f", msg.quantity, msg.symbol, msg.price)
            elif isinstance(msg, PortfolioUpdate):
                self.portfolio.apply_server_update(msg)
                logger.debug("Portfolio: net_worth=%.2f", msg.net_worth)
            elif isinstance(msg, SessionEvent):
                await self._on_session_event(msg)
            elif isinstance(msg, OrderAck):
                logger.debug("Ack %s %d %s @ %.4f",
                             msg.side, msg.quantity, msg.symbol, msg.price)
            elif isinstance(msg, ErrorMsg):
                logger.warning("Error [%s]: %s", msg.code, msg.message)
            elif isinstance(msg, Leaderboard):
                self._on_leaderboard(msg)

    async def _on_session_event(self, msg: SessionEvent) -> None:
        logger.info("Event: %s — %s", msg.event, msg.message)
        if msg.event == "SESSION_OPEN":
            self._session_open = True
            _console.print(
                f"[bold green]▶  Session open — {config.TEAM_ID} shock-reactive mode active[/bold green]"
            )
        elif msg.event == "SESSION_CLOSED":
            self._session_open = False
            _console.print("[bold red]■  Session closed — flattening positions[/bold red]")
            await self._flatten_all()
        elif msg.event == "SHOCK":
            shock_id = msg.data.get("shock_id", "")
            affected = msg.data.get("affected", [])
            prices   = msg.data.get("prices", {})
            _console.print(
                f"[bold yellow]⚡ SHOCK [{shock_id}]: {msg.message}[/bold yellow]"
            )
            self.strategy.on_shock(
                shock_id, affected, prices, self.market, self.portfolio
            )

    def _on_leaderboard(self, msg: Leaderboard) -> None:
        for entry in msg.traders:
            if entry.get("team_id") == config.TEAM_ID:
                sign = "+" if entry.get("realized_pnl", 0) >= 0 else ""
                _console.print(
                    f"[cyan]Tick {msg.tick}[/cyan]  "
                    f"rank=#{entry.get('rank', '?')}  "
                    f"net_worth=${entry.get('net_worth', 0):,.2f}  "
                    f"realized={sign}{entry.get('realized_pnl', 0):,.2f}"
                )
                break

    # ── Trading loop ──────────────────────────────────────────────────────────

    async def trading_loop(self) -> None:
        while True:
            await asyncio.sleep(config.TICK_INTERVAL_SEC)
            if not self._session_open:
                continue
            try:
                signal = self.strategy.generate_signal(self.market, self.portfolio)
            except Exception:
                logger.exception("Strategy error — skipping tick")
                continue
            if signal is None:
                continue
            await self._place_order(signal)

    async def _place_order(self, signal: Signal) -> None:
        await self._send(PlaceOrder(
            team_id=config.TEAM_ID,
            symbol=signal.symbol,
            side=signal.side,
            order_type="limit",
            price=signal.price,
            quantity=signal.quantity,
        ))
        logger.debug("Placed %s %d %s @ %.4f",
                     signal.side, signal.quantity, signal.symbol, signal.price)

    async def _flatten_all(self) -> None:
        for symbol, qty in list(self.portfolio.positions.items()):
            if qty == 0:
                continue
            await self._send(PlaceOrder(
                team_id=config.TEAM_ID,
                symbol=symbol,
                side="sell" if qty > 0 else "buy",
                order_type="market",
                price=0.0,
                quantity=abs(qty),
            ))

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        _console.print("[bold magenta]╔══════════════════════════════════════════════╗[/bold magenta]")
        _console.print("[bold magenta]║   AlgoArena — Shock Trader  (Team Quant)    ║[/bold magenta]")
        _console.print("[bold magenta]╠══════════════════════════════════════════════╣[/bold magenta]")
        _console.print(
            f"[bold magenta]║[/bold magenta]"
            f"  Team:     [bold]{config.TEAM_ID:<34}[/bold]"
            f"[bold magenta]║[/bold magenta]"
        )
        _console.print(
            f"[bold magenta]║[/bold magenta]"
            f"  Exchange: {config.EXCHANGE_URL:<34}"
            f"[bold magenta]║[/bold magenta]"
        )
        _console.print("[bold magenta]╚══════════════════════════════════════════════╝[/bold magenta]")
        _console.print()

        retry_delay = 3.0
        while True:
            try:
                await self.connect()
                self._session_open = False
                self.market = MarketData()
                await asyncio.gather(self.listen(), self.trading_loop())
            except websockets.exceptions.ConnectionClosed as exc:
                logger.warning("Connection closed: %s — retrying in %.0fs", exc, retry_delay)
            except OSError as exc:
                logger.warning("Cannot reach exchange: %s — retrying in %.0fs", exc, retry_delay)
            except Exception:
                logger.exception("Unexpected error — retrying in %.0fs", retry_delay)
            finally:
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(ShockTraderBot().run())
