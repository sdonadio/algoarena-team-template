"""
Trader configuration. Change TEAM_ID to your team name before running.

Environment variables:
    EXCHANGE_HOST   Override exchange hostname (default: localhost)
    EXCHANGE_PORT   Override exchange port (default: 8765)
"""

import os

TEAM_ID = os.environ.get("TEAM_ID", "trader_alpha")
EXCHANGE_URL = (
    f"ws://{os.environ.get('EXCHANGE_HOST', 'localhost')}"
    f":{os.environ.get('EXCHANGE_PORT', '8765')}"
)

# Team token from registration (hosted deployments; empty for local play).
ARENA_TOKEN = os.environ.get("ARENA_TOKEN", "")

STARTING_CASH = 100_000.0         # Starting cash — must match exchange config

# Symbols to trade. The exchange sends a BookSnapshot for every registered
# symbol on connect, so SYMBOLS is effectively populated automatically.
SYMBOLS: list[str] = ["AAPL", "TSLA", "BTC"]

TICK_INTERVAL_SEC = float(os.environ.get("TICK_INTERVAL", "0.5"))  # How often the trading loop fires

# Level 4 risk limits — used by RiskManager once you implement it.
MAX_POSITION_SIZE = 20            # Max shares held per symbol (long or short)
MAX_DAILY_LOSS = 5_000.0          # Halt trading if total loss exceeds this
STOP_LOSS_PCT = 0.02              # Close a position if it loses > 2% of entry
