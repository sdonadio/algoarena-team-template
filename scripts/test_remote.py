#!/usr/bin/env python
"""
scripts/test_remote.py — prove your connection to a hosted AlgoArena.

Runs the whole student path against a remote arena and prints a checklist:

  1. dashboard reachable          (HTTP)
  2. registration code accepted   (POST /api/register, validate_only)
  3. team registered              (a throwaway team, or your own token)
  4. handshake authenticated      (WebSocket + team token)
  5. market data flowing          (book snapshots arrive)
  6. order accepted               (limit order → OrderAck)
  7. trade executed               (broker quote crossed by trader → fill)
  8. portfolio updated            (cash/positions reflect the fill)

Usage — throwaway team (needs the class registration code):
    python scripts/test_remote.py --arena http://ARENA_HOST:8888 --code CODE

Usage — your real team (reads ARENA_TOKEN from .env or the environment;
skips registration, needs your bot ids):
    python scripts/test_remote.py --arena http://ARENA_HOST:8888 \
        --token $ARENA_TOKEN --broker my_team_broker --trader my_team_trader_1

Exit code 0 only if every step passes. This file is also a worked example
of the wire protocol (see docs/QUICKSTART.md and shared/messages.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
if (_ROOT / "engine").is_dir():        # student-template layout
    sys.path.insert(0, str(_ROOT / "engine"))

import websockets

from shared.messages import Handshake, PlaceOrder, parse_message

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return ok


def http_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, {}


async def recv_type(ws, wanted: str, timeout: float = 10.0):
    """Read frames until a message of the wanted type arrives (or timeout)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
        except asyncio.TimeoutError:
            break
        msg = parse_message(json.loads(raw))
        if msg.type == wanted:
            return msg
        if msg.type == "error":
            return msg
    return None


async def run(args: argparse.Namespace) -> int:
    arena = args.arena.rstrip("/")
    ws_host = arena.split("//")[-1].split(":")[0]
    ws_url = f"ws://{ws_host}:{args.port}"

    print(f"\nTesting arena {arena}  (exchange {ws_url})\n")

    # 1. dashboard reachable ------------------------------------------------
    try:
        with urllib.request.urlopen(arena, timeout=10) as resp:
            check("dashboard reachable", resp.status == 200, f"HTTP {resp.status}")
    except OSError as exc:
        check("dashboard reachable", False, str(exc))
        return finish()

    # 2/3. registration (throwaway team) or use the provided token ----------
    if args.token:
        token, broker_id, trader_id = args.token, args.broker, args.trader
        check("using provided team token", bool(broker_id and trader_id),
              "pass --broker and --trader with --token")
        if not (broker_id and trader_id):
            return finish()
    else:
        if not args.code:
            check("registration code provided", False, "pass --code (or --token)")
            return finish()
        status, body = http_json(f"{arena}/api/register",
                                 {"code": args.code, "validate_only": True})
        if not check("registration code accepted", status == 200,
                     body.get("error", "")):
            return finish()
        name = f"Smoke {uuid.uuid4().hex[:6]}"
        status, body = http_json(f"{arena}/api/register", {
            "code": args.code, "name": name,
            "broker_capitals": [100_000], "trader_capitals": [50_000],
        })
        if not check("team registered", status == 200,
                     body.get("team") or body.get("error", "")):
            return finish()
        token = body["token"]
        broker_id = body["broker_ids"][0]
        trader_id = body["trader_ids"][0]

    # 4/5. authenticate + market data ---------------------------------------
    async with websockets.connect(ws_url) as ws:
        await ws.send(Handshake(team_id=trader_id, role="trader",
                                level=1, token=token).model_dump_json())
        first = await recv_type(ws, "book_snapshot", timeout=10)
        authed = first is not None and first.type == "book_snapshot"
        det = "" if authed else (getattr(first, "code", None) or "no data — is a session open?")
        if not check("handshake authenticated", first is None or first.type != "error",
                     getattr(first, "code", "")):
            return finish()
        check("market data flowing", authed, det or f"{first.symbol} mid {first.mid_price}")
        ref = first.mid_price if authed else 100.0
        sym = first.symbol if authed else "AAPL"

    # 6/7/8. broker quotes, trader crosses, both see the results ------------
    async def broker():
        async with websockets.connect(ws_url) as ws:
            await ws.send(Handshake(team_id=broker_id, role="broker",
                                    level=1, token=token).model_dump_json())
            await asyncio.sleep(0.5)
            await ws.send(PlaceOrder(team_id=broker_id, symbol=sym, side="sell",
                                     order_type="limit", price=round(ref * 1.001, 2),
                                     quantity=2).model_dump_json())
            ack = await recv_type(ws, "order_ack", timeout=8)
            return ack is not None and ack.type == "order_ack"

    async def trader():
        await asyncio.sleep(1.5)      # let the broker's quote rest first
        async with websockets.connect(ws_url) as ws:
            await ws.send(Handshake(team_id=trader_id, role="trader",
                                    level=1, token=token).model_dump_json())
            await asyncio.sleep(0.3)
            await ws.send(PlaceOrder(team_id=trader_id, symbol=sym, side="buy",
                                     order_type="ioc", price=round(ref * 1.01, 2),
                                     quantity=2).model_dump_json())
            trade = await recv_type(ws, "trade_execution", timeout=8)
            pf = await recv_type(ws, "portfolio_update", timeout=8)
            return trade, pf

    ack_ok, (trade, pf) = await asyncio.gather(broker(), trader())
    check("order accepted (OrderAck)", ack_ok)
    traded = trade is not None and trade.type == "trade_execution"
    check("trade executed", traded,
          f"{trade.quantity} {trade.symbol} @ {trade.price} "
          f"(fee {trade.fee:.2f} / rebate {trade.maker_rebate:.2f})" if traded
          else "no fill — is the session open? (teacher must press START)")
    pf_ok = pf is not None and pf.type == "portfolio_update" and \
        pf.positions.get(sym, 0) >= 2
    check("portfolio updated", pf_ok,
          f"cash {pf.cash:,.0f}, {sym} position {pf.positions.get(sym)}" if pf_ok else "")

    return finish()


def finish() -> int:
    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"FAILED: {len(failed)} of {len(RESULTS)} checks — {', '.join(failed)}")
        return 1
    print(f"ALL {len(RESULTS)} CHECKS PASSED — you are ready to trade.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Test your connection to a hosted arena")
    ap.add_argument("--arena", required=True, help="dashboard URL, e.g. http://host:8888")
    ap.add_argument("--port", type=int, default=8765, help="exchange port (default 8765)")
    ap.add_argument("--code", help="class registration code (creates a throwaway team)")
    ap.add_argument("--token", help="your team token (skips registration)")
    ap.add_argument("--broker", help="your broker bot id (with --token)")
    ap.add_argument("--trader", help="your trader bot id (with --token)")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
