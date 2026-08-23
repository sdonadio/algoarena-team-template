#!/usr/bin/env python
"""
scripts/create_team.py — AlgoArena team creation wizard.

Every new team starts with the same budget and chooses how to invest it:

    Exchange license   $300,000   run your own matching venue, earn fees
    Broker desk        $100,000+  market-make, earn spread + maker rebates
    Trader seat         $50,000+  run an algo bot, maximize P&L

Two modes:

LOCAL (teacher machine — everything in one repo):
    python scripts/create_team.py
    → registers in teacher/teams.json, generates students/<team>/

REMOTE (student repo — hosted arena on AWS):
    python scripts/create_team.py --remote https://arena.example.edu
    → registers via the arena's API (you need the class registration code),
      saves your secret team token to .env, generates team/ starter code
    (in the student template repo this is just:  make register)

The exchange grants each bot its allocated capital at connection time,
so your allocation IS your starting cash.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teacher.registration import (  # noqa: E402
    DEFAULT_REBATE_BPS, DEFAULT_TAKER_BPS, EXCHANGE_LICENSE, MAX_BROKERS,
    MAX_TRADERS, MIN_BROKER, MIN_TRADER, TAKER_MAX_BPS, TAKER_MIN_BPS,
    TEAM_BUDGET, VENUE_NET_MIN_BPS, RegistrationError, load_roster,
    roster_entry, slugify, validate_plan,
)

STUDENTS_DIR = ROOT / "students"


def money(n: float) -> str:
    return f"${n:,.0f}"


# ── Interactive prompts ───────────────────────────────────────────────────────

def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or (default or "")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            v = int(raw.replace(",", "").replace("$", ""))
            if lo <= v <= hi:
                return v
            print(f"    → must be between {lo:,} and {hi:,}")
        except ValueError:
            print("    → enter a number")


def ask_float(prompt: str, default: float, lo: float, hi: float) -> float:
    while True:
        raw = ask(prompt, f"{default:g}")
        try:
            v = float(raw.replace(",", ""))
            if lo <= v <= hi:
                return v
            print(f"    → must be between {lo:g} and {hi:g}")
        except ValueError:
            print("    → enter a number")


def collect_payload(args: argparse.Namespace) -> dict:
    """Walk through the budget allocation. Returns the registration payload."""
    interactive = not args.yes

    print("━" * 62)
    print("  ALGOARENA — NEW TEAM SETUP")
    print("━" * 62)
    print(f"""
  Your team starts with a budget of {money(TEAM_BUDGET)}.
  Invest it across three kinds of seats:

    🏛  Exchange license  {money(EXCHANGE_LICENSE):>10}  run a venue, earn fees on every trade
    🏦  Broker desk       {money(MIN_BROKER):>10}+  quote two-sided markets, earn the
                                       spread + maker rebates from the exchange
    🤖  Trader seat       {money(MIN_TRADER):>10}+  run an algorithmic strategy

  Whatever you allocate to a broker/trader seat becomes that bot's
  starting cash. Unspent budget is wasted — invest it all!
""")

    name = args.name
    while not name:
        name = ask("Team name (e.g. 'Team Rocket')")

    budget = TEAM_BUDGET

    if interactive and args.exchange is None:
        want_exchange = ask(
            f"Buy an exchange license for {money(EXCHANGE_LICENSE)}? (y/n)", "n"
        ).lower().startswith("y")
    else:
        want_exchange = bool(args.exchange)
    taker_bps = args.taker_bps
    rebate_bps = args.rebate_bps
    if want_exchange:
        budget -= EXCHANGE_LICENSE
        print(f"    ✓ Exchange license — remaining budget {money(budget)}\n")
        # Your venue, your fee schedule — that is how exchanges compete.
        print(f"""  Your venue sets its own fee schedule. Every matched trade:
    the AGGRESSOR pays your taker fee, the RESTING side earns your maker
    rebate, and you keep the difference.

    A fat rebate attracts market makers (tighter quotes → more flow) but
    thins your margin. A high taker fee maximises revenue per trade until
    the flow goes to a cheaper venue.

    Bounds: taker {TAKER_MIN_BPS:g}–{TAKER_MAX_BPS:g} bps · rebate 0 bps up to
    (taker − {VENUE_NET_MIN_BPS:g}) so you always net at least
    {VENUE_NET_MIN_BPS:g} bps.
""")
        if interactive and taker_bps is None:
            taker_bps = ask_float("Taker fee (bps)", DEFAULT_TAKER_BPS,
                                  TAKER_MIN_BPS, TAKER_MAX_BPS)
        if taker_bps is None:
            taker_bps = DEFAULT_TAKER_BPS
        max_rebate = taker_bps - VENUE_NET_MIN_BPS
        if interactive and rebate_bps is None:
            rebate_bps = ask_float(f"Maker rebate (bps, 0–{max_rebate:g})",
                                   min(DEFAULT_REBATE_BPS, max_rebate),
                                   0.0, max_rebate)
        if rebate_bps is None:
            rebate_bps = min(DEFAULT_REBATE_BPS, max_rebate)
        print(f"    ✓ Schedule: taker {taker_bps:g} bps, rebate "
              f"{rebate_bps:g} bps → you net "
              f"{taker_bps - rebate_bps:g} bps per trade\n")

    max_brokers = min(MAX_BROKERS, budget // MIN_BROKER)
    n_brokers = args.brokers if args.brokers is not None else (
        ask_int(f"How many broker desks? (0–{max_brokers})",
                1 if max_brokers else 0, 0, max_brokers)
        if interactive and max_brokers else 0)
    broker_caps: list[int] = []
    for i in range(n_brokers):
        hi = budget - MIN_BROKER * (n_brokers - i - 1)
        default = args.broker_capital or min(hi, max(MIN_BROKER, budget // (n_brokers - i + 1)))
        cap = (ask_int(f"Capital for broker {i+1}? ({money(MIN_BROKER)}–{money(hi)})",
                       int(default), MIN_BROKER, hi)
               if interactive and args.broker_capital is None else
               min(int(args.broker_capital or MIN_BROKER), hi))
        broker_caps.append(cap)
        budget -= cap
        print(f"    ✓ Broker desk {i+1}: {money(cap)} — remaining {money(budget)}")

    max_traders = min(MAX_TRADERS, budget // MIN_TRADER)
    n_traders = args.traders if args.traders is not None else (
        ask_int(f"How many trader seats? (0–{max_traders})",
                min(2, max_traders), 0, max_traders)
        if interactive and max_traders else 0)
    trader_caps: list[int] = []
    for i in range(n_traders):
        hi = budget - MIN_TRADER * (n_traders - i - 1)
        default = args.trader_capital or min(hi, max(MIN_TRADER, budget // (n_traders - i)))
        cap = (ask_int(f"Capital for trader {i+1}? ({money(MIN_TRADER)}–{money(hi)})",
                       int(default), MIN_TRADER, hi)
               if interactive and args.trader_capital is None else
               min(int(args.trader_capital or MIN_TRADER), hi))
        trader_caps.append(cap)
        budget -= cap
        print(f"    ✓ Trader seat {i+1}: {money(cap)} — remaining {money(budget)}")

    if budget > 0:
        print(f"\n  ⚠ {money(budget)} left unallocated — this money is wasted.")

    payload = {"name": name, "exchange": want_exchange,
               "broker_capitals": broker_caps, "trader_capitals": trader_caps}
    if want_exchange:
        payload["taker_bps"] = taker_bps
        payload["rebate_bps"] = rebate_bps
    return payload


def confirm(plan: dict, interactive: bool) -> None:
    print("\n  ── YOUR TEAM ──────────────────────────────────────────")
    print(f"  {plan['name']}")
    if plan["exchange_port"]:
        print(f"    🏛  Exchange on port {plan['exchange_port']}  "
              f"(license {money(EXCHANGE_LICENSE)})")
        fees = plan.get("fees") or {}
        if fees:
            print(f"        fees: taker {fees['taker'] * 10_000:g} bps · "
                  f"rebate {fees['rebate'] * 10_000:g} bps · net "
                  f"{(fees['taker'] - fees['rebate']) * 10_000:g} bps")
    for bid in plan["broker_ids"]:
        print(f"    🏦  {bid:<24} {money(plan['capital'][bid])}")
    for tid in plan["trader_ids"]:
        print(f"    🤖  {tid:<24} {money(plan['capital'][tid])}")
    print("  ───────────────────────────────────────────────────────")
    if interactive:
        if not ask("Create this team? (y/n)", "y").lower().startswith("y"):
            sys.exit("  Aborted — nothing written.")


# ── Code generation ───────────────────────────────────────────────────────────

def write_package(plan: dict, pkg_dir: pathlib.Path, module: str) -> None:
    """Generate the starter code package for a team.

    module: import path of the package (e.g. "students.rocket" or "team").
    """
    name, slug = plan["name"], plan["slug"]
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f'"""{name} — AlgoArena team package."""\n')

    (pkg_dir / "config.py").write_text(f'''"""
{name} — team configuration.

Your bot IDs and capital allocation were set at registration.
The exchange grants each bot its allocated capital when it connects.

Environment variables:
    TEAM_ID         which bot this process is
    ARENA_TOKEN     your team's secret token (from registration — see .env)
    EXCHANGE_HOST   exchange hostname (default: localhost)
    EXCHANGE_PORT   exchange port     (default: 8765)
"""

import os

EXCHANGE_URL = (
    f"ws://{{os.environ.get('EXCHANGE_HOST', 'localhost')}}"
    f":{{os.environ.get('EXCHANGE_PORT', '8765')}}"
)
ARENA_TOKEN = os.environ.get("ARENA_TOKEN", "")

TEAM_NAME  = {name!r}
BROKER_IDS = {plan["broker_ids"]!r}
TRADER_IDS = {plan["trader_ids"]!r}

# Capital you allocated per bot (informational — the exchange enforces it):
CAPITAL = {plan["capital"]!r}
''')

    if plan["trader_ids"]:
        (pkg_dir / "trader.py").write_text(f'''"""
{name} — trader bot.

This starter runs the standard TraderBot plumbing with YOUR strategy
plugged in. Edit MyStrategy.generate_signal() — that is your edge.

Run one seat:
    TEAM_ID={plan["trader_ids"][0]} python -m {module}.trader
"""

from __future__ import annotations

import asyncio

from trader.trader import Signal, Strategy, TraderBot


class MyStrategy(Strategy):
    """Your alpha lives here."""

    def generate_signal(self, market, portfolio):
        # TODO: implement your strategy.
        #
        # You can read:
        #   market.prices[symbol]      current mid price
        #   market.history[symbol]     recent price history (deque)
        #   market.books[symbol]       latest book snapshot (bids/asks)
        #   portfolio.cash             your cash
        #   portfolio.positions        symbol → shares held
        #
        # Return a Signal(symbol=..., side="buy"/"sell", quantity=..., price=...)
        # to trade, or None to sit out this tick.
        return super().generate_signal(market, portfolio)


if __name__ == "__main__":
    bot = TraderBot()
    bot.strategy = MyStrategy()
    asyncio.run(bot.run())
''')

    if plan["broker_ids"]:
        (pkg_dir / "broker.py").write_text(f'''"""
{name} — broker (market maker).

Posts two-sided quotes and earns the spread plus maker rebates on every
passive fill. Inventory management (Level 4 quote skewing) is the key to
staying solvent — the exchange charges margin interest and liquidates
teams below maintenance.

Run:
    TEAM_ID={plan["broker_ids"][0]} python -m {module}.broker
"""

from __future__ import annotations

import asyncio

from broker.broker import BrokerBot


class MyBroker(BrokerBot):
    """Your market-making logic. Override quoting methods here."""
    # TODO Level 4: skew your quotes based on inventory.


if __name__ == "__main__":
    asyncio.run(MyBroker().run())
''')

    lines = [f"# {name}", "", f"Budget invested: ${TEAM_BUDGET:,}", ""]
    if plan["exchange_port"]:
        lines += [f"- 🏛 Exchange license — your venue is assigned port "
                  f"{plan['exchange_port']}", ""]
    for bid in plan["broker_ids"]:
        lines += [f"- 🏦 `{bid}` (${plan['capital'][bid]:,}):", "",
                  f"      TEAM_ID={bid} python -m {module}.broker", ""]
    for tid in plan["trader_ids"]:
        lines += [f"- 🤖 `{tid}` (${plan['capital'][tid]:,}):", "",
                  f"      TEAM_ID={tid} python -m {module}.trader", ""]
    lines += ["## Where to write code", "",
              "- `trader.py` → `MyStrategy.generate_signal()` — your edge",
              "- `broker.py` → quoting and inventory management",
              "- `config.py` → your tunables"]
    (pkg_dir / "README.md").write_text("\n".join(lines) + "\n")


# ── Local and remote registration ─────────────────────────────────────────────

def register_local(payload: dict) -> None:
    import shared.auth as auth
    from teacher.registration import ROSTER_PATH, _save_roster

    roster = load_roster()
    plan = validate_plan(payload, roster)
    if (STUDENTS_DIR / plan["slug"]).exists():
        sys.exit(f"  ✗ students/{plan['slug']}/ already exists")
    confirm(plan, interactive=sys.stdin.isatty() and not payload.get("_yes"))

    roster[plan["name"]] = roster_entry(plan, module_prefix=f"students.{plan['slug']}")
    _save_roster(roster)
    token = auth.issue_token(plan["name"])
    (STUDENTS_DIR / "__init__.py").touch(exist_ok=True)
    write_package(plan, STUDENTS_DIR / plan["slug"], f"students.{plan['slug']}")

    print(f"""
  ✓ Team created!

  Your code:    students/{plan["slug"]}/
  Registered:   teacher/teams.json
  Team token:   {token}
                (only needed when the exchange runs with AUTH_REQUIRED)
""")


def preflight_code(url: str, code: str) -> None:
    """Check the class code against the server before the wizard walk.

    Newer servers answer a validate_only request after the code gate and
    before any other validation. Older servers don't know validate_only and
    fall through to name validation — any error OTHER than a code rejection
    therefore means the code itself was accepted, so we proceed.
    """
    body = json.dumps({"code": code, "validate_only": True}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/register",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        print("  ✓ registration code accepted\n")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:
            detail = ""
        if "registration code" in detail.lower() or "disabled" in detail.lower():
            sys.exit(f"  ✗ {detail}")
        print("  ✓ registration code accepted\n")   # older server, code passed
    except OSError as exc:
        sys.exit(f"  ✗ Could not reach {url}: {exc}")


def register_remote(payload: dict, url: str, code: str) -> None:
    body = json.dumps({**payload, "code": code}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/register",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:
            detail = ""
        sys.exit(f"  ✗ Registration rejected: {detail or exc}")
    except OSError as exc:
        sys.exit(f"  ✗ Could not reach {url}: {exc}")

    plan = {**result, "name": result["team"]}
    host = url.split("//")[-1].split("/")[0].split(":")[0]

    # Save credentials (gitignored) and generate the starter package.
    env = pathlib.Path(".env")
    env.write_text(
        f"# {result['team']} — issued at registration. Do NOT commit or share.\n"
        f"ARENA_TOKEN={result['token']}\n"
        f"EXCHANGE_HOST={host}\n"
    )
    write_package(plan, pathlib.Path("team"), "team")

    print(f"""
  ✓ Team {result["team"]!r} registered with the arena!

  Credentials:  .env               ← your secret token; never commit it
  Your code:    team/              ← write your strategy here
  Connect:      set the env vars from .env, then e.g.
                TEAM_ID={(result["trader_ids"] or result["broker_ids"] or ["<bot>"])[0]} \\
                    ARENA_TOKEN=... EXCHANGE_HOST={host} python -m team.trader
""")
    if result.get("exchange_port"):
        print(f"  Your exchange is assigned port {result['exchange_port']} — "
              f"ask your teacher to deploy it.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Create an AlgoArena team")
    ap.add_argument("--name")
    ap.add_argument("--exchange", action="store_true", default=None)
    ap.add_argument("--brokers", type=int, default=None)
    ap.add_argument("--traders", type=int, default=None)
    ap.add_argument("--broker-capital", type=int, default=None)
    ap.add_argument("--trader-capital", type=int, default=None)
    ap.add_argument("--taker-bps", type=float, default=None,
                    help=f"your venue's taker fee in bps "
                         f"({TAKER_MIN_BPS:g}–{TAKER_MAX_BPS:g}, "
                         f"default {DEFAULT_TAKER_BPS:g}); needs --exchange")
    ap.add_argument("--rebate-bps", type=float, default=None,
                    help=f"your venue's maker rebate in bps (0 to taker−"
                         f"{VENUE_NET_MIN_BPS:g}, default "
                         f"{DEFAULT_REBATE_BPS:g}); needs --exchange")
    ap.add_argument("--remote", metavar="URL",
                    help="register with a hosted arena (e.g. https://arena.example.edu)")
    ap.add_argument("--code", help="class registration code (remote mode)")
    ap.add_argument("--yes", action="store_true", help="non-interactive; accept defaults")
    args = ap.parse_args()

    try:
        # Remote mode: verify the class code BEFORE walking the student
        # through the whole budget allocation — a typo used to be reported
        # only after every question was answered.
        code = None
        if args.remote:
            code = args.code or ask("Class registration code")
            preflight_code(args.remote, code)

        payload = collect_payload(args)

        if args.remote:
            register_remote(payload, args.remote, code)
        else:
            payload["_yes"] = args.yes
            register_local(payload)
    except RegistrationError as exc:
        sys.exit(f"  ✗ {exc}")


if __name__ == "__main__":
    main()
