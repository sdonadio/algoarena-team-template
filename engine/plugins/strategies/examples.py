"""
Example trading strategies for AlgoArena.

These are teaching examples: readability matters more than performance.
Import this module to register them on the global arena.

Each strategy follows the signal_fn signature:
    (symbol, prices, history, book, portfolio) -> Signal | None

Arguments:
    symbol    — the security being considered
    prices    — dict[str, float] of all current prices
    history   — list[float] of past prices for this symbol (oldest first)
    book      — OrderBook instance (or None / snapshot dict)
    portfolio — dict with keys: cash, positions, realized_pnl, net_worth, …
"""

from __future__ import annotations

from shared.messages import Signal
from plugins import arena


# ------------------------------------------------------------------
# Strategy implementations
# ------------------------------------------------------------------

def _momentum_signal(
    symbol: str, prices: dict, history: list, book, portfolio: dict
) -> Signal | None:
    """Moving-average crossover momentum (5-tick vs 20-tick).

    Logic:
        short_MA = average of the most recent 5 prices
        long_MA  = average of the most recent 20 prices

        If short_MA > long_MA the trend is bullish  → BUY
        If short_MA < long_MA the trend is bearish  → SELL
        Otherwise no signal.

    Requires at least 20 ticks of history.
    """
    if len(history) < 20 or symbol not in prices:
        return None

    short_ma = sum(history[-5:]) / 5
    long_ma = sum(history[-20:]) / 20
    price = prices[symbol]

    if short_ma > long_ma * 1.001:      # small buffer to avoid whipsaw
        return Signal(symbol=symbol, side="buy", quantity=10, price=price)
    elif short_ma < long_ma * 0.999:
        return Signal(symbol=symbol, side="sell", quantity=10, price=price)
    return None


def _mean_reversion_signal(
    symbol: str, prices: dict, history: list, book, portfolio: dict
) -> Signal | None:
    """Z-score mean-reversion fade (window = 20 ticks, threshold = 1.5σ).

    Logic:
        Compute the mean and standard deviation of the last 20 prices.
        z = (current_price - mean) / std

        z > +1.5  → overbought → SELL (expect reversion downward)
        z < -1.5  → oversold  → BUY  (expect reversion upward)

    Requires at least 20 ticks of history.
    """
    if len(history) < 20 or symbol not in prices:
        return None

    window = history[-20:]
    mean = sum(window) / 20
    variance = sum((x - mean) ** 2 for x in window) / 19  # sample variance
    if variance == 0:
        return None
    std = variance ** 0.5

    z = (prices[symbol] - mean) / std
    if z > 1.5:
        return Signal(symbol=symbol, side="sell", quantity=10, price=prices[symbol])
    elif z < -1.5:
        return Signal(symbol=symbol, side="buy", quantity=10, price=prices[symbol])
    return None


def _book_imbalance_signal(
    symbol: str, prices: dict, history: list, book, portfolio: dict
) -> Signal | None:
    """Order book imbalance — trade in the direction of excess liquidity.

    Logic:
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        ranges from -1 (all asks) to +1 (all bids).

        imbalance > +0.4  → institutional buyers present → BUY
        imbalance < -0.4  → institutional sellers present → SELL

    Requires an OrderBook object with order_book_imbalance().
    Confidence is set to the absolute imbalance value.
    """
    if symbol not in prices or book is None:
        return None
    try:
        imbalance = book.order_book_imbalance()
    except AttributeError:
        return None

    price = prices[symbol]
    if imbalance > 0.4:
        return Signal(symbol=symbol, side="buy", quantity=5, price=price,
                      confidence=imbalance)
    elif imbalance < -0.4:
        return Signal(symbol=symbol, side="sell", quantity=5, price=price,
                      confidence=abs(imbalance))
    return None


def _pairs_trader_signal(
    symbol: str, prices: dict, history: list, book, portfolio: dict
) -> Signal | None:
    """Relative-value pairs trade: symbol vs equal-weighted portfolio average.

    Logic:
        market_avg = mean of all current prices in the universe
        ratio = prices[symbol] / market_avg

        ratio < 0.95  → symbol is cheap relative to the market → BUY
        ratio > 1.05  → symbol is expensive relative to the market → SELL

    Note: this simplified version uses raw prices, so it works best when all
    securities are in a similar price range. Real pairs trading correlates
    specific asset pairs (e.g. BTC vs ETH) using normalized returns.
    """
    if not prices or symbol not in prices:
        return None
    market_avg = sum(prices.values()) / len(prices)
    if market_avg == 0:
        return None

    price = prices[symbol]
    ratio = price / market_avg

    if ratio < 0.95:
        confidence = min(1.0, (0.95 - ratio) * 20)
        return Signal(symbol=symbol, side="buy", quantity=10, price=price,
                      confidence=confidence)
    elif ratio > 1.05:
        confidence = min(1.0, (ratio - 1.05) * 20)
        return Signal(symbol=symbol, side="sell", quantity=10, price=price,
                      confidence=confidence)
    return None


def _do_nothing_signal(
    symbol: str, prices: dict, history: list, book, portfolio: dict
) -> Signal | None:
    """Baseline do-nothing strategy: never trades.

    Useful as a control condition. A team running this strategy earns zero
    P&L from trading, revealing the full cost of fees paid by other strategies.
    """
    return None


# ------------------------------------------------------------------
# Register example strategies on the global arena
# ------------------------------------------------------------------

arena.register_strategy(
    id="momentum", name="MA Crossover Momentum",
    description="Buy when 5-tick MA crosses above 20-tick MA; sell when it crosses below.",
    color="#60a5fa",
    signal_fn=_momentum_signal,
)

arena.register_strategy(
    id="mean_reversion", name="Z-Score Mean Reversion",
    description="Fade 1.5σ moves: sell overbought, buy oversold (20-tick window).",
    color="#f472b6",
    signal_fn=_mean_reversion_signal,
)

arena.register_strategy(
    id="book_imbalance", name="Order Book Imbalance",
    description="Trade in the direction of imbalance when it exceeds ±0.4.",
    color="#fb923c",
    signal_fn=_book_imbalance_signal,
)

arena.register_strategy(
    id="pairs_trader", name="Pairs / Relative Value",
    description="Buy when price < 95% of market average; sell when > 105%.",
    color="#818cf8",
    signal_fn=_pairs_trader_signal,
)

arena.register_strategy(
    id="do_nothing", name="Do Nothing (Baseline)",
    description="Never trades. Baseline for measuring fee drag.",
    color="#94a3b8",
    signal_fn=_do_nothing_signal,
)

# Public aliases — import these directly in sim_session and your own tests.
momentum_signal = _momentum_signal
mean_reversion_signal = _mean_reversion_signal
book_imbalance_signal = _book_imbalance_signal
pairs_trader_signal = _pairs_trader_signal
do_nothing_signal = _do_nothing_signal
