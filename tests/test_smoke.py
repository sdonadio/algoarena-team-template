"""tests/test_smoke.py — Week 0 "Green Smoke Test" (Deliverable 1).

Run it:

    python -m pytest tests/test_smoke.py -v

Six checks that prove your machine is ready for Week 1. Checks that need an
external resource (your Anthropic key, the network, the class arena) SKIP —
not fail — until you configure them, so `-v` shows you exactly what is left to
set up. When everything is configured, all six are green; submit that
screenshot to Canvas.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest


def test_python_version():
    """1. Python 3.11+ virtual environment is active."""
    assert sys.version_info[:2] >= (3, 11), (
        f"Need Python 3.11+, but this is {sys.version.split()[0]} — "
        "activate your 3.11 virtual environment."
    )


def test_arena_sdk_imports():
    """2. Arena SDK installed — arena.Trader imports successfully."""
    from arena import Signal, Trader  # noqa: F401

    assert Trader is not None


def test_anthropic_key_and_response():
    """3. ANTHROPIC_API_KEY set — Claude responds to a test prompt."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("set ANTHROPIC_API_KEY (and `pip install anthropic`) to run the Claude check")
    anthropic = pytest.importorskip("anthropic", reason="run `pip install anthropic`")
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
        )
    except Exception as exc:  # noqa: BLE001 — surface the real reason to the student
        pytest.skip(f"ANTHROPIC_API_KEY is set but Claude did not respond ({exc}) — check the key/network")
    assert resp.content, "Claude returned an empty response"


def test_yfinance_live_price():
    """4. yfinance returns a live AAPL price."""
    yf = pytest.importorskip("yfinance", reason="run `pip install yfinance`")
    try:
        hist = yf.Ticker("AAPL").history(period="1d")
        price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"yfinance could not fetch a price (network/rate-limit?): {exc}")
    if price <= 0:
        pytest.skip("yfinance returned no price (market data unavailable right now)")
    assert price > 0


def test_engine_and_sdk_smoke():
    """5. Arena engine + SDK smoke: the order book matches and a full
    no-network session runs end to end."""
    # Engine: a crossing order prints a trade at the resting price.
    from shared.orderbook import OrderBook

    ob = OrderBook("AAPL")
    ob.place_order("maker", "sell", 100.0, 5)
    _order, trades = ob.place_order("taker", "buy", 100.0, 5)
    assert trades and trades[0].price == 100.0

    # SDK + engine end to end, no network required (tests/sim_session.py).
    try:
        from sim_session import SimSession          # tests/ is on the path under pytest
    except ImportError:
        from tests.sim_session import SimSession
    result = SimSession().run(n_ticks=50, verbose=False)
    assert result is not None


def test_exchange_connection():
    """6. Bot connects to the exchange and market data flows.

    Set EXCHANGE_HOST (and EXCHANGE_PORT, default 8765) to the class arena to
    run this; it connects, handshakes, and waits for a book snapshot.
    """
    host = os.environ.get("EXCHANGE_HOST")
    if not host:
        pytest.skip("set EXCHANGE_HOST / EXCHANGE_PORT to the class arena to run the live connect check")
    port = os.environ.get("EXCHANGE_PORT", "8765")
    websockets = pytest.importorskip("websockets")

    async def _probe() -> bool:
        url = f"ws://{host}:{port}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "type": "handshake",
                "team_id": os.environ.get("TEAM_ID", "smoke_check"),
                "role": "observer",
                "level": 1,
            }))
            for _ in range(60):
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                if json.loads(raw).get("type") == "book_snapshot":
                    return True
        return False

    try:
        ok = asyncio.run(asyncio.wait_for(_probe(), timeout=20))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"EXCHANGE_HOST is set but couldn't reach the arena at {host}:{port} ({exc}) — check the URL/token")
    assert ok, "connected to the arena but no market data arrived"
