"""
Crypto securities for AlgoArena — BTC-USD and ETH-USD (Real-Time Intelligent
Systems, week 5: Distributed Systems & Crypto).

They trade on the ordinary CLOB like any other symbol. Nothing about the
matching engine is crypto-specific; what changes is the *system* around them:

  * the reference market never closes, so a broker polling Yahoo Finance sees
    a moving price on a Sunday night when every equity is frozen
  * the price level is three orders of magnitude above the equities, so a
    single coin is most of a starting balance — position sizing stops being
    an afterthought
  * two venues quoting the same coin walk apart (exchange/config.py
    MID_BLEND_WEIGHT), which is the week-5 lab: measure the basis, find the
    arbitrage window, then add latency and partition a link

Opt-in: the exchange, broker and sync-prices tool load this module only when
CRYPTO_SECURITIES=true, so the equity-only courses are unaffected. Yahoo
Finance serves both tickers under exactly these symbols, so a broker quotes
them with no code change.

Plugin contract (CLAUDE.md): a security is a price function
(prev_price, tick, params) -> float with no side effects.
"""

from __future__ import annotations

import plugins.securities.defaults as defaults  # noqa: F401  (equities first)
from plugins import arena

CRYPTO_SYMBOLS: tuple[str, ...] = ("BTC-USD", "ETH-USD")

# Base prices are the level a venue opens at when data/base_prices.json is
# absent; `make sync-prices` (with CRYPTO_SECURITIES=true) overwrites them
# with the live reference. Volatility is annualised, like the equities'.
arena.register_security(
    id="BTC-USD", name="Bitcoin / US Dollar", asset_type="crypto",
    base_price=81000.00, color="#f7931a",
    price_fn=defaults.make_fundamental("BTC-USD", sigma=1.5), vol=1.5,
)

arena.register_security(
    id="ETH-USD", name="Ether / US Dollar", asset_type="crypto",
    base_price=4000.00, color="#627eea",
    price_fn=defaults.make_fundamental("ETH-USD", sigma=1.9), vol=1.9,
)
