"""tests/test_smoke.py — Week 0 "Green Smoke Test" (Deliverable 1).

Run it:

    python -m pytest tests/test_smoke.py -v

Six checks that prove your machine is ready for Week 1. Checks that need an
external resource (the network, the class arena) SKIP — not fail — until you
configure them. Read the skips carefully:

    A SKIP IS NOT A GREEN CHECK. It means the check never ran.

Check 3 (generative AI) is OPTIONAL. Use any provider you like (Anthropic,
OpenAI, Gemini, ...) or none at all; nothing graded depends on it. With no
provider key set, check 3 PASSES and says so. With a key set, it verifies that
provider actually answers, so a broken key is caught now and not in Week 3.

The run prints its own summary line ("4 passed, 2 skipped — a skip is NOT a
green check") so there is no way to mistake a partly-configured machine for a
finished one. Deliverable 1 is 6 passed, 0 skipped; that is the screenshot
Canvas wants.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from shared.messages import BookSnapshot, Handshake, parse_message

# Number of checks in this file. Deliverable 1 is all of them PASSED.
SMOKE_CHECKS = 6


def smoke_handshake() -> Handshake:
    """The handshake check 6 sends to the arena.

    Built as a Pydantic model so it is validated before it hits the socket
    and round-trips through parse_message() on the exchange side.
    """
    return Handshake(
        team_id=os.environ.get("TEAM_ID", "smoke_check"),
        role="observer",
        level=1,
        token=os.environ.get("ARENA_TOKEN", ""),
    )


def format_summary(passed: int, skipped: int, failed: int = 0) -> str:
    """Render the end-of-run summary line for the Week-0 smoke test.

    Skipped checks are the whole point of this wording: pytest prints them in
    yellow next to the green dots, and students read "no failures" as "done".
    They are not done — a skipped check never ran.

    Args:
        passed:  Checks that ran and passed.
        skipped: Checks that were skipped (unconfigured resource).
        failed:  Checks that ran and failed.

    Returns:
        A one-or-two-line string, ready to print.
    """
    head = f"Week-0 smoke test: {passed} passed, {skipped} skipped"
    if failed:
        head += f", {failed} FAILED"

    if skipped or failed:
        return (
            f"{head} — a skip is NOT a green check.\n"
            f"    {passed}/{SMOKE_CHECKS} checks actually ran green. "
            f"Deliverable 1 needs {SMOKE_CHECKS}/{SMOKE_CHECKS}: read each "
            f"SKIPPED line above, configure what it names, and run this again."
        )
    return (
        f"{head} — all {SMOKE_CHECKS}/{SMOKE_CHECKS} checks ran green. "
        f"Screenshot this for Canvas."
    )


@pytest.fixture(scope="module", autouse=True)
def _smoke_summary(request):
    """Print format_summary() after the last check, skips included.

    pytest's own tally ("4 passed, 2 skipped") is easy to misread as a pass,
    so this restates it in words. Written defensively: a change in pytest's
    internals must never make the smoke test itself fail.
    """
    yield
    try:
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        here = str(request.node.fspath)

        def _count(outcome: str) -> int:
            return sum(
                1 for rep in reporter.stats.get(outcome, [])
                if getattr(rep, "fspath", None)
                and here.endswith(str(rep.fspath))
            )

        line = format_summary(_count("passed"), _count("skipped"),
                              _count("failed"))
        # Suspend pytest's capture so the line reaches the real terminal
        # instead of the captured-output buffer of the last test.
        capman = request.config.pluginmanager.get_plugin("capturemanager")
        if capman is not None:
            capman.suspend_global_capture(in_=True)
        try:
            reporter.write_line("")
            for row in line.split("\n"):
                reporter.write_line(row)
        finally:
            if capman is not None:
                capman.resume_global_capture()
    except Exception:  # noqa: BLE001 — a broken summary must not fail the run
        pass


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


def _ai_ping(provider: str) -> str:
    """Send one tiny prompt to `provider` and return its text reply.

    Args:
        provider: "anthropic", "openai" or "gemini" (chosen from the env key).

    Returns:
        The model's reply text (may be empty if the provider returned nothing).
    """
    prompt = "Reply with the single word OK."
    if provider == "anthropic":
        import anthropic
        resp = anthropic.Anthropic().messages.create(
            model=os.environ.get("AI_SMOKE_MODEL", "claude-haiku-4-5"),
            max_tokens=8, messages=[{"role": "user", "content": prompt}])
        return "".join(getattr(b, "text", "") for b in resp.content)
    if provider == "openai":
        import openai
        resp = openai.OpenAI().chat.completions.create(
            model=os.environ.get("AI_SMOKE_MODEL", "gpt-4o-mini"),
            max_tokens=8, messages=[{"role": "user", "content": prompt}])
        return resp.choices[0].message.content or ""
    from google import genai  # provider == "gemini"
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    resp = genai.Client(api_key=key).models.generate_content(
        model=os.environ.get("AI_SMOKE_MODEL", "gemini-2.0-flash"), contents=prompt)
    return resp.text or ""


# provider -> (env vars that select it, pip package, import name)
_AI_PROVIDERS = {
    "anthropic": (("ANTHROPIC_API_KEY",), "anthropic", "anthropic"),
    "openai":    (("OPENAI_API_KEY",), "openai", "openai"),
    "gemini":    (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "google-genai", "google.genai"),
}


def test_ai_provider_optional():
    """3. Generative AI is OPTIONAL — if you configured a provider, it answers.

    No key set: PASS (AI is not required in this course). A key set for
    Anthropic, OpenAI or Gemini: the provider must answer a one-word prompt.
    To opt out after setting a key, unset it and run again.
    """
    import importlib.util

    chosen = [name for name, (envs, _, _) in _AI_PROVIDERS.items()
              if any(os.environ.get(e) for e in envs)]
    if not chosen:
        print("\n    check 3: no AI provider key set — fine, generative AI is optional "
              "in this course (any provider, or none).")
        return
    for name in chosen:
        envs, pkg, mod = _AI_PROVIDERS[name]
        if importlib.util.find_spec(mod.split(".")[0]) is None or (
                "." in mod and importlib.util.find_spec(mod) is None):
            pytest.fail(f"{envs[0]} is set but `{pkg}` is not installed: "
                        f"`pip install {pkg}`, or unset {envs[0]} to opt out of AI")
        try:
            reply = _ai_ping(name)
        except Exception as exc:  # noqa: BLE001 — surface the real reason to the student
            pytest.fail(f"{envs[0]} is set but {name} did not answer ({exc}). "
                        f"Fix the key/network, or unset {envs[0]} to opt out of AI.")
        assert reply.strip(), f"{name} returned an empty reply"
        print(f"\n    check 3: {name} answered: {reply.strip()[:20]!r}")


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

    # SDK + engine end to end, no network required (sim/session.py).
    # `sim` is a real package, so this import cannot be shadowed by a
    # third-party `tests` package the way `tests.sim_session` could.
    from sim.session import SimSession

    result = SimSession().run(n_ticks=50, verbose=False)
    assert result is not None


def test_exchange_connection():
    """6. Bot connects to the exchange and market data flows.

    Dials the same URL your bot uses: EXCHANGE_URL (e.g. the hosted
    wss://feed.… address `make register` wrote to .env), else
    EXCHANGE_HOST + EXCHANGE_PORT. It connects, sends a
    shared.messages.Handshake, and waits for a BookSnapshot.
    """
    from shared.exchange_url import SOURCE_DEFAULT, resolve_exchange_url

    url, source = resolve_exchange_url()
    if source == SOURCE_DEFAULT:
        pytest.skip("set EXCHANGE_URL (or EXCHANGE_HOST / EXCHANGE_PORT) to the class arena "
                    "— `make register` writes it to .env — to run the live connect check")
    websockets = pytest.importorskip("websockets")

    async def _probe() -> bool:
        async with websockets.connect(url) as ws:
            # Every message on the wire is a Pydantic model from
            # shared/messages.py — never a hand-rolled dict (CLAUDE.md rule 1).
            await ws.send(smoke_handshake().model_dump_json())
            for _ in range(60):
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                if isinstance(parse_message(json.loads(raw)), BookSnapshot):
                    return True
        return False

    try:
        ok = asyncio.run(asyncio.wait_for(_probe(), timeout=20))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"couldn't reach the arena at {url} ({source}): {exc} — check the URL/token")
    assert ok, "connected to the arena but no market data arrived"
