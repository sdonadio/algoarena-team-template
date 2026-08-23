"""
Default market shock events for AlgoArena.

Importing this module registers 10 built-in shocks on the global arena.
Each shock is a function: (prices, securities, params) -> ShockResult.
"""

from __future__ import annotations

import random

from shared.messages import ShockResult
from plugins import arena


# ------------------------------------------------------------------
# Shock implementations
# ------------------------------------------------------------------

def _earnings_beat(prices: dict, securities: list, params: dict) -> ShockResult:
    """One random equity surges 3-6% on a surprise earnings beat."""
    equities = [s for s in securities if s.asset_type == "equity"]
    if not equities:
        return ShockResult(prices=dict(prices), message="No equities to affect", affected=[])
    target = random.choice(equities)
    pct = random.uniform(0.03, 0.06)
    new_prices = dict(prices)
    new_prices[target.id] = prices.get(target.id, target.base_price) * (1 + pct)
    return ShockResult(
        prices=new_prices,
        message=f"{target.name} earnings beat: +{pct * 100:.1f}%",
        affected=[target.id],
    )


def _earnings_miss(prices: dict, securities: list, params: dict) -> ShockResult:
    """One random equity falls 4-8% on a disappointing earnings miss."""
    equities = [s for s in securities if s.asset_type == "equity"]
    if not equities:
        return ShockResult(prices=dict(prices), message="No equities to affect", affected=[])
    target = random.choice(equities)
    pct = random.uniform(0.04, 0.08)
    new_prices = dict(prices)
    new_prices[target.id] = prices.get(target.id, target.base_price) * (1 - pct)
    return ShockResult(
        prices=new_prices,
        message=f"{target.name} earnings miss: -{pct * 100:.1f}%",
        affected=[target.id],
    )


def _flash_crash(prices: dict, securities: list, params: dict) -> ShockResult:
    """All assets plunge 3-6% simultaneously in a sudden liquidity event."""
    pct = random.uniform(0.03, 0.06)
    new_prices = {sid: p * (1 - pct) for sid, p in prices.items()}
    return ShockResult(
        prices=new_prices,
        message=f"Flash crash: all assets -{pct * 100:.1f}%",
        affected=list(prices.keys()),
    )


def _risk_on_rally(prices: dict, securities: list, params: dict) -> ShockResult:
    """Risk appetite surges — all assets rally 2-4%."""
    pct = random.uniform(0.02, 0.04)
    new_prices = {sid: p * (1 + pct) for sid, p in prices.items()}
    return ShockResult(
        prices=new_prices,
        message=f"Risk-on rally: all assets +{pct * 100:.1f}%",
        affected=list(prices.keys()),
    )


def _fed_rate_hike(prices: dict, securities: list, params: dict) -> ShockResult:
    """Fed raises rates: equities/ETFs -3%, crypto -7%."""
    equity_ids = {s.id for s in securities if s.asset_type in ("equity", "etf")}
    crypto_ids = {s.id for s in securities if s.asset_type == "crypto"}
    new_prices = dict(prices)
    affected = []
    for sid in equity_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 0.97
            affected.append(sid)
    for sid in crypto_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 0.93
            affected.append(sid)
    return ShockResult(
        prices=new_prices,
        message="Fed rate hike: equities/ETFs -3%, crypto -7%",
        affected=affected,
    )


def _fed_rate_cut(prices: dict, securities: list, params: dict) -> ShockResult:
    """Fed cuts rates: equities/ETFs +2%, crypto +5%."""
    equity_ids = {s.id for s in securities if s.asset_type in ("equity", "etf")}
    crypto_ids = {s.id for s in securities if s.asset_type == "crypto"}
    new_prices = dict(prices)
    affected = []
    for sid in equity_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 1.02
            affected.append(sid)
    for sid in crypto_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 1.05
            affected.append(sid)
    return ShockResult(
        prices=new_prices,
        message="Fed rate cut: equities/ETFs +2%, crypto +5%",
        affected=affected,
    )


def _sector_rotate(prices: dict, securities: list, params: dict) -> ShockResult:
    """Rotation out of crypto into equities: crypto -5%, equities +2.5%."""
    equity_ids = {s.id for s in securities if s.asset_type == "equity"}
    crypto_ids = {s.id for s in securities if s.asset_type == "crypto"}
    new_prices = dict(prices)
    affected = []
    for sid in crypto_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 0.95
            affected.append(sid)
    for sid in equity_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 1.025
            affected.append(sid)
    return ShockResult(
        prices=new_prices,
        message="Sector rotation: crypto -5%, equities +2.5%",
        affected=affected,
    )


def _geo_crisis(prices: dict, securities: list, params: dict) -> ShockResult:
    """Geopolitical crisis: commodities +8%, risk assets (equities/crypto/ETFs) -5%."""
    commodity_ids = {s.id for s in securities if s.asset_type == "commodity"}
    risk_ids = {s.id for s in securities if s.asset_type in ("equity", "crypto", "etf")}
    new_prices = dict(prices)
    affected = []
    for sid in commodity_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 1.08
            affected.append(sid)
    for sid in risk_ids:
        if sid in prices:
            new_prices[sid] = prices[sid] * 0.95
            affected.append(sid)
    return ShockResult(
        prices=new_prices,
        message="Geo crisis: commodities +8%, risk assets -5%",
        affected=affected,
    )


def _vol_spike(prices: dict, securities: list, params: dict) -> ShockResult:
    """VIX spike: each asset moves independently ±4-8% in a random direction."""
    new_prices = {}
    affected = []
    for sid, price in prices.items():
        pct = random.uniform(0.04, 0.08)
        direction = random.choice([1, -1])
        new_prices[sid] = price * (1 + direction * pct)
        affected.append(sid)
    return ShockResult(
        prices=new_prices,
        message="Volatility spike: each asset ±4-8% independently",
        affected=affected,
    )


def _liquidity_crunch(prices: dict, securities: list, params: dict) -> ShockResult:
    """Bid-ask spreads widen dramatically. No direct price effect — brokers should re-quote."""
    return ShockResult(
        prices=dict(prices),  # prices unchanged
        message="Liquidity crunch: spreads widening. Brokers should widen quotes immediately.",
        affected=[],
    )


# ------------------------------------------------------------------
# Register built-in shocks on the global arena
# ------------------------------------------------------------------

arena.register_shock(
    id="earnings_beat", label="Earnings Beat",
    description="A random equity surprises with strong earnings (+3-6%)",
    category="earnings",
    apply_fn=_earnings_beat,
)

arena.register_shock(
    id="earnings_miss", label="Earnings Miss",
    description="A random equity disappoints on earnings (-4-8%)",
    category="earnings",
    apply_fn=_earnings_miss,
)

arena.register_shock(
    id="flash_crash", label="Flash Crash",
    description="Sudden all-asset selloff (-3-6%)",
    category="systemic",
    apply_fn=_flash_crash,
)

arena.register_shock(
    id="risk_on_rally", label="Risk-On Rally",
    description="Risk appetite surges, all assets rally (+2-4%)",
    category="macro",
    apply_fn=_risk_on_rally,
)

arena.register_shock(
    id="fed_rate_hike", label="Fed Rate Hike",
    description="Fed raises rates: equities -3%, crypto -7%",
    category="macro",
    apply_fn=_fed_rate_hike,
)

arena.register_shock(
    id="fed_rate_cut", label="Fed Rate Cut",
    description="Fed cuts rates: equities +2%, crypto +5%",
    category="macro",
    apply_fn=_fed_rate_cut,
)

arena.register_shock(
    id="sector_rotate", label="Sector Rotation",
    description="Out of crypto into equities: crypto -5%, equities +2.5%",
    category="allocation",
    apply_fn=_sector_rotate,
)

arena.register_shock(
    id="geo_crisis", label="Geopolitical Crisis",
    description="Safe-haven bid: commodities +8%, risk assets -5%",
    category="geopolitical",
    apply_fn=_geo_crisis,
)

arena.register_shock(
    id="vol_spike", label="Volatility Spike",
    description="VIX surge: each asset moves ±4-8% independently",
    category="volatility",
    apply_fn=_vol_spike,
)

arena.register_shock(
    id="liquidity_crunch", label="Liquidity Crunch",
    description="Spreads widen dramatically — no direct price effect",
    category="liquidity",
    apply_fn=_liquidity_crunch,
)
