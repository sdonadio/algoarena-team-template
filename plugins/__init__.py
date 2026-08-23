"""
ArenaRegistry — central plugin registry for AlgoArena.

All securities, shocks, and strategies are stored here. The global `arena`
instance is shared by all other modules; import it to register plugins.
"""

from __future__ import annotations

import logging
from typing import Callable

from shared.messages import SecurityDef, ShockResult, Signal

logger = logging.getLogger(__name__)


class ArenaRegistry:
    """Holds all registered plugins. One global instance: arena = ArenaRegistry()."""

    def __init__(self) -> None:
        # Internal storage: id → metadata + callable
        self.securities: dict[str, dict] = {}
        self.shocks: dict[str, dict] = {}
        self.strategies: dict[str, dict] = {}
        # Current prices for every registered security (updated by tick_prices / apply_shock)
        self.prices: dict[str, float] = {}
        # Last tick advanced, so a repeated call for the same tick is a no-op
        # (see tick_prices) rather than a second step of the walk.
        self._last_tick: int | None = None

    # ------------------------------------------------------------------
    # Security plugins
    # ------------------------------------------------------------------

    def register_security(
        self,
        id: str,
        name: str,
        asset_type: str,
        base_price: float,
        color: str,
        price_fn: Callable,
        vol: float | None = None,
        tick_order: int = 0,
    ) -> None:
        """Register a tradeable security with its price function.

        price_fn signature: (prev_price: float, tick: int, params: dict) -> float

        tick_order: securities are priced in ascending order of this value
        each tick. Derived instruments (an index, a basket) must use a higher
        value than the things they read, or they would be computed from last
        tick's constituents. Relying on registration order would silently
        break the moment an import order changed.
        """
        self.securities[id] = {
            "defn": SecurityDef(
                id=id, name=name, asset_type=asset_type,
                base_price=base_price, color=color,
            ),
            "price_fn": price_fn,
            "vol": vol,
            "current_price": base_price,
            "tick_order": int(tick_order),
        }
        self.prices[id] = base_price

    def reset_tick(self) -> None:
        """Forget the last advanced tick — a new session/season restarts at 1."""
        self._last_tick = None

    def unregister_security(self, id: str) -> None:
        """Remove a security. Raises KeyError if not registered."""
        del self.securities[id]
        self.prices.pop(id, None)

    def list_securities(self) -> list[SecurityDef]:
        """Return SecurityDef metadata for all registered securities."""
        return [entry["defn"] for entry in self.securities.values()]

    # ------------------------------------------------------------------
    # Shock plugins
    # ------------------------------------------------------------------

    def register_shock(
        self,
        id: str,
        label: str,
        description: str,
        category: str,
        apply_fn: Callable,
    ) -> None:
        """Register a market shock event.

        apply_fn signature: (prices: dict, securities: list, params: dict) -> ShockResult
        """
        self.shocks[id] = {
            "label": label,
            "description": description,
            "category": category,
            "apply_fn": apply_fn,
        }

    def unregister_shock(self, id: str) -> None:
        """Remove a shock. Raises KeyError if not registered."""
        del self.shocks[id]

    def list_shocks(self) -> list[dict]:
        """Return metadata (without apply_fn) for all registered shocks."""
        return [
            {
                "id": id_,
                "label": e["label"],
                "description": e["description"],
                "category": e["category"],
            }
            for id_, e in self.shocks.items()
        ]

    def apply_shock(self, shock_id: str, params: dict | None = None) -> ShockResult:
        """Apply a registered shock to current prices. Raises KeyError if not found.

        Updates self.prices and each security's current_price with the result.
        """
        if params is None:
            params = {}
        if shock_id not in self.shocks:
            raise KeyError(f"Unknown shock: {shock_id!r}")
        result: ShockResult = self.shocks[shock_id]["apply_fn"](
            self.prices, self.list_securities(), params
        )
        for sec_id, new_price in result.prices.items():
            if sec_id in self.securities:
                self.prices[sec_id] = new_price
                self.securities[sec_id]["current_price"] = new_price
        return result

    # ------------------------------------------------------------------
    # Strategy plugins
    # ------------------------------------------------------------------

    def register_strategy(
        self,
        id: str,
        name: str,
        description: str,
        color: str,
        signal_fn: Callable,
    ) -> None:
        """Register an algorithmic trading strategy.

        signal_fn signature: (symbol, prices, history, book, portfolio) -> Signal | None
        """
        self.strategies[id] = {
            "name": name,
            "description": description,
            "color": color,
            "signal_fn": signal_fn,
        }

    def unregister_strategy(self, id: str) -> None:
        """Remove a strategy. Raises KeyError if not registered."""
        del self.strategies[id]

    def list_strategies(self) -> list[dict]:
        """Return metadata (without signal_fn) for all registered strategies."""
        return [
            {
                "id": id_,
                "name": e["name"],
                "description": e["description"],
                "color": e["color"],
            }
            for id_, e in self.strategies.items()
        ]

    def get_signal(
        self,
        strategy_id: str,
        symbol: str,
        prices: dict,
        history: list,
        book,
        portfolio: dict,
    ) -> Signal | None:
        """Call a strategy's signal function, returning None on any exception.

        A broken strategy MUST NOT crash the engine — all exceptions are caught
        and logged; the caller receives None as if no signal was generated.

        Raises KeyError if strategy_id is not registered.
        """
        if strategy_id not in self.strategies:
            raise KeyError(f"Unknown strategy: {strategy_id!r}")
        try:
            return self.strategies[strategy_id]["signal_fn"](
                symbol, prices, history, book, portfolio
            )
        except Exception:
            logger.exception("Strategy %r raised — returning None", strategy_id)
            return None

    # ------------------------------------------------------------------
    # Engine tick
    # ------------------------------------------------------------------

    def tick_prices(self, tick: int) -> dict[str, float]:
        """Advance every security's price by one tick.

        Calls each price_fn with the security's current price. Exceptions
        inside a price_fn are caught and logged; the previous price is kept
        so one bad plugin cannot halt the simulation.

        Derived securities (higher tick_order) are priced after the ones they
        read, so an index never lags its constituents.

        IDEMPOTENT per tick: calling this twice with the same `tick` advances
        the walk once and returns the same prices. Several ExchangeServers can
        share one registry (the simulator runs N venues in one process), and
        each of them calls this once per tick — without this, N venues would
        take N steps of the shared fundamental every tick.

        Returns a fresh copy of the updated prices dict.
        """
        if tick == self._last_tick:
            return dict(self.prices)
        self._last_tick = tick
        ordered = sorted(self.securities.items(),
                         key=lambda kv: kv[1].get("tick_order", 0))
        for sec_id, entry in ordered:
            try:
                new_price = float(entry["price_fn"](entry["current_price"], tick, {}))
                new_price = max(new_price, 0.01)  # floor at 1 cent
            except Exception:
                logger.exception("price_fn for %r raised — keeping previous price", sec_id)
                new_price = entry["current_price"]
            entry["current_price"] = new_price
            self.prices[sec_id] = new_price
        return dict(self.prices)


# Global instance shared by all modules.
arena = ArenaRegistry()
