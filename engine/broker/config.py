"""
Broker configuration. Change TEAM_ID to your team name before running.

Environment variables:
    EXCHANGE_HOST       Override exchange hostname (default: localhost)
    EXCHANGE_PORT       Override exchange port (default: 8765)
    TEAM_ID             This broker's identifier
"""

import os

TEAM_ID = os.environ.get("TEAM_ID", "broker_alpha")
EXCHANGE_URL = (
    f"ws://{os.environ.get('EXCHANGE_HOST', 'localhost')}"
    f":{os.environ.get('EXCHANGE_PORT', '8765')}"
)

# Multi-venue (Level 6): comma-separated list of exchange URLs to quote on
# simultaneously. One BrokerBot instance runs per venue.
#   EXCHANGE_URLS=ws://localhost:8765,ws://localhost:8766 python -m broker.broker
EXCHANGE_URLS: list[str] = (
    os.environ.get("EXCHANGE_URLS", EXCHANGE_URL).split(",")
)

# Team token from registration (hosted deployments; empty for local play).
ARENA_TOKEN = os.environ.get("ARENA_TOKEN", "")


# Symbols to market-make. Must match what the exchange has registered.
EQUITY_SYMBOLS: list[str] = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "NFLX", "AMD", "INTC"]

# Yahoo Finance poll interval in seconds (yfinance has no WebSocket feed).
YAHOO_POLL_INTERVAL: float = float(os.environ.get("YAHOO_POLL_INTERVAL", "5"))

# Level 1: fixed $ spread posted around the mid price.
BASE_SPREAD = 0.30      # e.g. bid = mid - 0.15, ask = mid + 0.15
# …but never wider than this fraction of the price. A flat dollar spread is
# 14 bps of a $220 share and 136 bps of a $22 one; the cap keeps the quoted
# market realistic across a 30x price range. Floor keeps it above the tick.
MAX_SPREAD_BPS = float(os.environ.get("MAX_SPREAD_BPS", "25"))
MIN_SPREAD_ABS = float(os.environ.get("MIN_SPREAD_ABS", "0.02"))
QUOTE_SIZE = 10         # shares/units per side — big enough to survive several fills
REQUOTE_INTERVAL_SEC = 0.5  # how often to refresh quotes (seconds)

# Seconds for the quote centre to close half the gap to the external (Yahoo)
# reference. Slow on purpose: Yahoo is a 5-second poll of a different market,
# so anchoring fast would import its staleness as noise, while not anchoring at
# all would let the venue drift away from the real security forever.
REFERENCE_HALFLIFE = float(os.environ.get("REFERENCE_HALFLIFE", "45"))

# How far the centre may drift before we requote, in BASIS POINTS of price.
# Relative, not absolute: a flat $0.10 threshold is 45 bps of a $22 share and
# 1.4 bps of a $720 one, so it made cheap names quote almost never (their
# resting quote could sit ~0.9% away from the next desk's, and the touch
# jumped between the two) while expensive names requoted on nothing at all.
# 5 bps of staleness is roughly a tick or two on a large cap.
REQUOTE_THRESHOLD_BPS = float(os.environ.get("REQUOTE_THRESHOLD_BPS", "5"))
# Absolute floor in dollars, so we never chase sub-penny noise.
REQUOTE_THRESHOLD = float(os.environ.get("REQUOTE_THRESHOLD", "0.01"))
# Age at which a quote is refreshed even in a flat market (nothing rests
# forever, and it re-establishes a quote if an ack or a reject went missing).
QUOTE_MAX_AGE_SEC = float(os.environ.get("QUOTE_MAX_AGE_SEC", "30"))

# DEPRECATED (Aug 2026): the broker no longer runs a private random walk.
# Intraday movement comes from order flow at the venue (trade impact) and from
# the shared fundamental, not from noise injected per broker per venue — that
# was measured adding a ~0.06% random jump every 0.5s to every symbol and
# pulling venues apart. Setting these env vars is harmless; they are ignored.
# See docs/SEASON_GUIDE.md "How a price is formed".
INTRADAY_SIGMA = float(os.environ.get("INTRADAY_SIGMA", "0"))      # ignored
REVERSION_HALFLIFE = float(os.environ.get("REVERSION_HALFLIFE", "60"))  # ignored
DEPRECATED_ENV = [name for name in ("INTRADAY_SIGMA", "REVERSION_HALFLIFE")
                  if name in os.environ]

# TODO Level 3: Volatility-adjusted spread
# MIN_SPREAD = 0.05     # tightest spread allowed in calm markets
# MAX_SPREAD = 2.00     # widest spread allowed in volatile markets
# VOL_WINDOW = 20       # number of ticks for rolling vol estimate
# VOL_MULTIPLIER = 5.0  # sensitivity: spread += VOL_MULTIPLIER * realized_vol

# TODO Level 4: Inventory management
# MAX_POSITION = 50     # max shares per side before hard skew kicks in
# SKEW_FACTOR = 0.01   # $ per share of net position to skew quotes

# TODO Level 5: Toxic flow detection
# TOXICITY_THRESHOLD = -0.50   # avg P&L impact per fill before flagging a trader
# TOXICITY_WINDOW = 20         # number of recent fills to consider per counterparty
