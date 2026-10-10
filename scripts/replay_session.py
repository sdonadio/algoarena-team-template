#!/usr/bin/env python
"""
scripts/replay_session.py — Replay a recorded AlgoArena session.

Serves a recorded session (sessions/session_*.jsonl, written automatically
by the exchange between SESSION_OPEN and SESSION_CLOSED) as a fake exchange
WebSocket. Point the web dashboard at it and watch the whole session again —
trades, leaderboard, flow animation — for post-mortems and TCA homework.

Usage:
    python scripts/replay_session.py sessions/session_20260820_141530.jsonl
    python scripts/replay_session.py <file> --speed 5      # 5× faster
    python scripts/replay_session.py <file> --port 8765 --loop
    python scripts/replay_session.py --latest              # newest recording

Then in another terminal:  make webdashboard
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import pathlib
import sys

import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(path: str) -> list[tuple[float, str]]:
    """Read a JSONL recording → [(ts, serialized_message), …]."""
    out: list[tuple[float, str]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                # `aux` records are audit-only (owner order acks/rejects,
                # cancels, connect/disconnect) — they were never broadcast, so
                # replaying them would put messages on the wire the live
                # session never sent. scripts/live_readiness.py reads them.
                if rec.get("aux"):
                    continue
                out.append((float(rec["ts"]), json.dumps(rec["msg"])))
            except (ValueError, KeyError):
                continue
    if not out:
        sys.exit(f"No messages found in {path}")
    return out


class ReplayServer:
    def __init__(self, messages: list[tuple[float, str]], speed: float, loop: bool):
        self.messages = messages
        self.speed = max(0.1, speed)
        self.loop = loop
        self.clients: set = set()

    async def handle_client(self, ws) -> None:
        self.clients.add(ws)
        print(f"  client connected ({len(self.clients)} total)")
        try:
            async for _ in ws:      # consume handshakes/commands, ignore
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def stream(self) -> None:
        while True:
            t0 = self.messages[0][0]
            start = asyncio.get_event_loop().time()
            for i, (ts, payload) in enumerate(self.messages):
                target = (ts - t0) / self.speed
                delay = target - (asyncio.get_event_loop().time() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
                dead = set()
                for c in self.clients:
                    try:
                        await c.send(payload)
                    except Exception:
                        dead.add(c)
                self.clients -= dead
                if i and i % 500 == 0:
                    print(f"  replayed {i}/{len(self.messages)} messages")
            print("  ── replay finished ──")
            if not self.loop:
                # Keep serving so late clients still get a session view;
                # stream is over but the process stays up.
                await asyncio.Event().wait()
            print("  looping…")

    async def run(self, port: int) -> None:
        span = self.messages[-1][0] - self.messages[0][0]
        print("━" * 56)
        print("  ALGOARENA SESSION REPLAY")
        print(f"  {len(self.messages)} messages · {span/60:.1f} min of market time "
              f"· speed ×{self.speed:g}")
        print(f"  Serving as fake exchange on ws://localhost:{port}")
        print("  → start the dashboard:  make webdashboard")
        print("━" * 56)
        async with websockets.serve(self.handle_client, "0.0.0.0", port):
            await self.stream()


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a recorded AlgoArena session")
    ap.add_argument("file", nargs="?", help="sessions/session_*.jsonl")
    ap.add_argument("--latest", action="store_true", help="replay the newest recording")
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--loop", action="store_true", help="restart from the top when done")
    args = ap.parse_args()

    path = args.file
    if args.latest or not path:
        candidates = sorted(glob.glob(str(ROOT / "sessions" / "session_*.jsonl")))
        if not candidates:
            sys.exit("No recordings found in sessions/ — run a session first")
        path = candidates[-1]
        print(f"  Using latest recording: {path}")

    server = ReplayServer(load(path), args.speed, args.loop)
    try:
        asyncio.run(server.run(args.port))
    except KeyboardInterrupt:
        print("\n  replay stopped")


if __name__ == "__main__":
    main()
