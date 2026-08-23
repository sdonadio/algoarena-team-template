"""
arena.exchange — derive Exchange, set your policy. The venue runs itself.

An exchange competes on exactly three things: the fees it publishes, the
orders it is willing to accept, and the reliability of its plant. The first
two are your hooks; the third is on you (keep the process up). Matching,
settlement, market data, and the dashboard feed are all provided by the
engine underneath (exchange/server.py).
"""

from __future__ import annotations

import asyncio
import logging

import websockets

import exchange.config as _config
from exchange.server import ExchangeServer, _print_startup
from shared.messages import ErrorMsg, PlaceOrder, TradeExecution

logger = logging.getLogger(__name__)


class Exchange:
    """Your venue. Subclass, set your fee schedule, veto what you must.

    Example — a discount venue that refuses oversized orders:

        class MyExchange(Exchange):
            taker_bps  = 10.0     # published to everyone (/api/venues)
            rebate_bps = 7.0

            def accept_order(self, order, portfolio):
                if order.quantity > 200:
                    return False, "max 200 shares per order on this venue"
                return True, ""

        if __name__ == "__main__":
            MyExchange().run()

    Run with:  EXCHANGE_PORT=<your assigned port> python -m team.exchange

    Fees are clamped to the class bounds (taker within the teacher's
    min/max; your rebate can never exceed taker minus the venue-net floor),
    so no schedule you pick can break the game — only lose it.
    """

    #: Published fee schedule, in basis points of notional. None → the
    #: class defaults. Undercut to attract flow; charge premium to earn
    #: more per trade. This trade-off IS the exchange game.
    taker_bps: float | None = None
    rebate_bps: float | None = None

    # ── The hooks (override these) ──────────────────────────────────────

    def accept_order(self, order: PlaceOrder, portfolio) -> tuple[bool, str]:
        """Inspect every order before it reaches the book.

        Return (True, "") to accept or (False, "reason") to reject — the
        reason is sent back to the trader. Runs before the engine's own
        checks. Default: accept everything.
        """
        return True, ""

    def on_trade(self, symbol: str, price: float, quantity: int,
                 buyer_id: str, seller_id: str) -> None:
        """Called after every trade that prints on your venue.

        Default: nothing. Use it for your own analytics — surveillance,
        volume stats, whatever makes your venue worth its fees.
        """

    # ── Entry point (don't override) ────────────────────────────────────

    def run(self) -> None:
        """Apply the fee schedule and run the venue until interrupted."""
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%H:%M:%S")
        self._apply_fees()
        try:
            asyncio.run(self._serve())
        except KeyboardInterrupt:
            pass

    def _apply_fees(self) -> None:
        """Clamp the published schedule into the class bounds and install it."""
        if self.taker_bps is not None:
            taker = min(max(float(self.taker_bps), _config.TAKER_MIN_BPS),
                        _config.TAKER_MAX_BPS)
            if taker != self.taker_bps:
                logger.warning("taker_bps clamped to %.1f (bounds %.1f–%.1f)",
                               taker, _config.TAKER_MIN_BPS, _config.TAKER_MAX_BPS)
            _config.TAKER_FEE_RATE = taker / 10_000.0
        if self.rebate_bps is not None:
            cap = _config.TAKER_FEE_RATE * 10_000.0 - _config.VENUE_NET_MIN_BPS
            rebate = min(max(float(self.rebate_bps), _config.REBATE_MIN_BPS), cap)
            if rebate != self.rebate_bps:
                logger.warning("rebate_bps clamped to %.1f (max taker − %.1f)",
                               rebate, _config.VENUE_NET_MIN_BPS)
            _config.MAKER_REBATE_RATE = rebate / 10_000.0

    async def _serve(self) -> None:
        server = _AdaptedExchangeServer(self)
        async with websockets.serve(server.handle_client,
                                    _config.HOST, _config.PORT):
            _print_startup(server)
            try:
                await asyncio.gather(
                    server._book_snapshot_loop(),
                    server._leaderboard_loop(),
                    server._price_tick_loop(),
                    server._teacher_cli(),
                )
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                pass
        logger.info("Venue stopped.")


# ── Adapter: bolt the policy onto the standard engine ────────────────────

class _AdaptedExchangeServer(ExchangeServer):
    def __init__(self, policy: Exchange) -> None:
        super().__init__()
        self._policy = policy

    async def _handle_place_order(self, ws, msg: PlaceOrder, team_id: str) -> None:
        try:
            ok, reason = self._policy.accept_order(msg, self.portfolios.get(team_id))
        except Exception:
            logger.exception("accept_order raised — order accepted by default")
            ok, reason = True, ""
        if not ok:
            await self._send(ws, ErrorMsg(
                code="VENUE_REJECTED",
                message=reason or "Rejected by venue policy"))
            return
        await super()._handle_place_order(ws, msg, team_id)

    async def _broadcast(self, message, **kwargs) -> None:
        await super()._broadcast(message, **kwargs)
        if isinstance(message, TradeExecution):
            try:
                self._policy.on_trade(message.symbol, message.price,
                                      message.quantity, message.buyer_id,
                                      message.seller_id)
            except Exception:
                logger.exception("on_trade raised — ignored")
