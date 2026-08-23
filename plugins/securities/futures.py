"""
Index futures for AlgoArena — the ARENA-10 contract (week 9).

ARENA-10 is a cash-settled future on the equal-weighted average of the ten
listed equities. It trades on the ordinary CLOB like any other symbol, but the
exchange treats it as a future (see exchange/config.FUTURES):

  * buying a contract posts margin instead of paying the notional
  * open positions are marked to the index and settled in CASH every
    FUTURES_SETTLE_TICKS ticks
  * it receives no starting-share grant and pays no dividends

Why it matters pedagogically: it is the brokers' first true hedge. A market
maker long a basket of single names can sell the index against it and keep the
spread income while shedding most of the directional risk. Before week 9 the
only way to reduce inventory risk was to stop quoting.

Registered on import, but only *tradeable* when the week's `futures_enabled`
flag is on.
"""

from __future__ import annotations

import plugins.securities.defaults  # noqa: F401  (the index members)
from plugins import arena

# The ten equities from plugins/securities/defaults.py, equally weighted.
INDEX_MEMBERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
    "GOOGL", "META", "NFLX", "AMD", "INTC",
)

ARENA10 = "ARENA10"


def index_level(prices: dict[str, float]) -> float | None:
    """Equal-weighted average of the members present in `prices`."""
    values = [prices[s] for s in INDEX_MEMBERS if s in prices and prices[s] > 0]
    if not values:
        return None
    return sum(values) / len(values)


def arena10_price_fn(prev_price: float, tick: int, params: dict) -> float:
    """Price function for ARENA-10: read the members straight off the registry.

    ARENA-10 registers with tick_order=1, so the registry prices it after
    every member: by the time this runs the members already carry this tick's
    price and the index never lags them.
    """
    level = index_level(arena.prices)
    return level if level is not None else prev_price


# Base level = the equal-weighted average of the members' base prices, read
# from the registry rather than restated here so the two cannot drift apart.
_BASE = index_level({
    sym: entry["defn"].base_price
    for sym, entry in arena.securities.items()
}) or 290.20

arena.register_security(
    id=ARENA10,
    name="ARENA-10 Index Future",
    asset_type="future",
    base_price=round(_BASE, 2),
    color="#facc15",
    price_fn=arena10_price_fn,
    vol=None,          # derived from its members, not drawn independently
    # Priced AFTER the members every tick, so the index never lags them.
    # Registration order must not matter — see ArenaRegistry.tick_prices.
    tick_order=1,
)
