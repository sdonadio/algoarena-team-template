#!/usr/bin/env python
"""
scripts/season_sim.py — AlgoArena season simulator / balance harness.

Runs a population of bots against the REAL exchange mechanics with no
network and no sleeps, then prints a season report. This is the regression
harness for the whole roadmap: after every phase, re-run it and check that
market making is still survivable, that the control benchmark is still
beatable, and that shock foreknowledge is worth a sensible amount.

Examples
--------
    # default field, 3 random shocks, reproducible
    python scripts/season_sim.py --seed 7

    # a longer season with more events
    python scripts/season_sim.py --ticks 4000 --shocks 6 --seed 1

    # run the real week scenario files (Phase 1) in sequence
    python scripts/season_sim.py --weeks 1-10 --seed 1

    # only the timing-only predictor (no direction foreknowledge)
    python scripts/season_sim.py --no-insider --seed 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.table import Table

from sim.arena import HeadlessArena, ScheduledShock
from sim.bots import BOT_PRESETS, DEFAULT_POPULATION, make_population
import exchange.scoring as scoring
from sim.report import render


# ---------------------------------------------------------------------------
# Lineups — "who wins with THIS field?"
# ---------------------------------------------------------------------------

def parse_lineup(spec: str) -> list[tuple[str, dict]]:
    """'momentum:2,passive_mm:1' → a population spec with generated bot ids.

    Bot ids are `<preset>_<n>` so the report reads the way the teacher typed
    the lineup. A bare preset name means one of them.

    Raises ValueError (with the valid presets listed) on anything unknown —
    a typo in a lineup should never silently run a different experiment.
    """
    out: list[tuple[str, dict]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        preset, _, count_raw = part.partition(":")
        preset = preset.strip()
        if preset not in BOT_PRESETS:
            raise ValueError(
                f"Unknown bot preset {preset!r}. Valid presets: "
                f"{', '.join(sorted(BOT_PRESETS))}")
        try:
            count = int(count_raw) if count_raw.strip() else 1
        except ValueError:
            raise ValueError(
                f"Bad count in {part!r} — use preset:count, e.g. "
                f"'{preset}:2'") from None
        if count < 1:
            raise ValueError(f"Count in {part!r} must be at least 1")
        for i in range(count):
            out.append((preset, {"team_id": f"{preset}_{i + 1}"}))
    if not out:
        raise ValueError("Empty lineup — give at least one preset:count")
    return out


def shock_ids() -> list[str]:
    """Every registered shock plugin id (plugins/shocks/defaults.py)."""
    import plugins.shocks.defaults  # noqa: F401  (registers on import)
    from plugins import arena as registry
    return [s["id"] for s in registry.list_shocks()]


# ---------------------------------------------------------------------------
# Shock schedules
# ---------------------------------------------------------------------------

def typed_schedule(shock_id: str, n: int, ticks: int,
                   warmup: int = 200) -> list[ScheduledShock]:
    """`n` firings of ONE registered shock, spread over the season."""
    out: list[ScheduledShock] = []
    if n <= 0 or ticks <= warmup:
        return out
    span = ticks - warmup
    for i in range(n):
        tick = warmup + int(span * (i + 0.5) / n)
        out.append(ScheduledShock(tick=tick, shock_id=shock_id,
                                  label=f"{shock_id} t{tick}"))
    return out

def random_schedule(
    n: int, ticks: int, symbols: list[str], warmup: int = 200,
) -> list[ScheduledShock]:
    """Spread `n` shocks over the season, alternating symbol-specific and
    market-wide, with magnitudes in the range real news actually produces."""
    out: list[ScheduledShock] = []
    if n <= 0 or ticks <= warmup:
        return out
    span = ticks - warmup
    for i in range(n):
        tick = warmup + int(span * (i + 0.5) / n)
        market_wide = (i % 3 == 2)
        pct = random.choice([-0.10, -0.07, -0.05, 0.05, 0.07, 0.10])
        if market_wide:
            pct *= 0.5
            out.append(ScheduledShock(tick=tick, pct=pct, symbol=None,
                                      label=f"MKT {pct:+.0%} t{tick}"))
        else:
            sym = random.choice(symbols)
            out.append(ScheduledShock(tick=tick, pct=pct, symbol=sym,
                                      label=f"{sym} {pct:+.0%} t{tick}"))
    return out


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

async def run_once(
    ticks: int,
    shocks: list[ScheduledShock],
    symbols: list[str] | None,
    seed: int | None,
    insider: bool,
    lead: int,
    post: int,
    console: Console,
    title: str = "SEASON REPORT",
    scenario=None,
    upgrades: dict | None = None,
    quiet: bool = False,
    fire_shocks: bool = True,
    lineup: list[tuple[str, dict]] | None = None,
    venues: int = 1,
    split_brain: bool = False,
) -> dict:
    """Build a population, run a season, print the report."""
    spec = []
    for preset, kwargs in (lineup or DEFAULT_POPULATION):
        kwargs = dict(kwargs)
        if preset == "shock_predictor":
            kwargs["lead"] = lead
            kwargs["post"] = post
            if not insider:
                kwargs["insider"] = False
        spec.append((preset, kwargs))

    bots = make_population(spec, schedule=shocks, symbols=symbols)
    arena = HeadlessArena(bots, shocks=shocks, symbols=symbols, seed=seed,
                          scenario=scenario, upgrades=upgrades,
                          fire_shocks=fire_shocks, venues=venues,
                          split_brain=split_brain)
    try:
        await arena.open_session()
        await arena.run(ticks)
        await arena.close_session()
        if quiet:
            return arena.summary()
        return render(arena, console=console, lead=lead, post=post, title=title)
    finally:
        arena.close()


async def compare_upgrade(
    key: str, target: str, args: argparse.Namespace, console: Console,
) -> None:
    """Run the same seeded season with and without one upgrade.

    Answers the question a team actually asks in a purchase window: is this
    worth the price? Both runs use the same seed, so the difference in the
    target bot's final net worth is the upgrade's contribution — compare it
    against the catalog price.
    """
    import exchange.upgrades as upgrade_mod

    if key not in upgrade_mod.CATALOG:
        console.print(f"[red]Unknown upgrade {key!r}. Choose from: "
                      f"{', '.join(upgrade_mod.CATALOG)}[/red]")
        return
    item = upgrade_mod.CATALOG[key]
    price = float(item["price"])

    symbols = args.symbols
    resolved = symbols or _all_symbols()
    shocks = random_schedule(args.shocks, args.ticks, resolved)

    def run_arm(granted):
        return run_once(
            ticks=args.ticks, shocks=list(shocks), symbols=symbols,
            seed=args.seed, insider=not args.no_insider, lead=args.lead,
            post=args.post, console=console, upgrades=granted, quiet=True,
        )

    base = await run_arm(None)
    with_up = await run_arm({target: [key]})

    def row_of(summary, team_id):
        return next((r for r in summary["bots"] if r["team_id"] == team_id), None)

    a, b = row_of(base, target), row_of(with_up, target)
    if a is None or b is None:
        console.print(f"[red]No bot named {target!r} in the population. "
                      f"Choose from: "
                      f"{', '.join(r['team_id'] for r in base['bots'])}[/red]")
        return

    delta = b["net_worth"] - a["net_worth"]
    console.print()
    console.rule(f"[bold]UPGRADE ROI — {item['label']}[/bold]")
    console.print(f"  target [bold]{target}[/bold] ({a['preset']})   "
                  f"seed {args.seed}   ticks {args.ticks:,}")
    console.print(f"  effect: [dim]{item['effect']}[/dim]")
    console.print()
    table = Table(header_style="bold")
    table.add_column("arm")
    table.add_column("net worth", justify="right")
    table.add_column("fees", justify="right")
    table.add_column("rebates", justify="right")
    table.add_column("carry", justify="right")
    table.add_column("fills", justify="right")
    for name, r in (("baseline", a), (f"with {key}", b)):
        table.add_row(name, f"${r['net_worth']:,.0f}", f"${r['fees']:,.0f}",
                      f"${r['rebates']:,.0f}", f"${r['carry']:,.0f}",
                      f"{r['trades']:,}")
    console.print(table)

    # An upgrade is permanent, so the decision horizon is every remaining
    # session — not the one week it was bought in.
    horizon = max(1, args.sessions_remaining)
    season_delta = delta * horizon
    verdict = "WORTH IT" if season_delta > price else "NOT WORTH IT"
    colour = "green" if season_delta > price else "red"
    console.print()
    console.print(f"  gain this session      [bold]${delta:,.0f}[/bold]  "
                  f"({delta / price:.2f}x price)")
    console.print(f"  gain over {horizon} sessions   "
                  f"[bold]${season_delta:,.0f}[/bold]")
    console.print(f"  upgrade price          ${price:,.0f}")
    console.print(f"  verdict                [{colour}]{verdict}[/{colour}]  "
                  f"(season payback {season_delta / price:.2f}x)")
    console.print("  [dim]One seed is one sample — re-run with several seeds "
                  "before changing a price. Note the simulated bots also cap "
                  "their own size, so a position-limit upgrade may not bind."
                  "[/dim]")


async def compare_hedge(args: argparse.Namespace, console: Console) -> None:
    """Compare a market maker that hedges with ARENA-10 against one that does not.

    Same seed, same flow, same quotes; the only difference is whether the
    market maker sells the index against its inventory. What we want to see is
    the hedged book carrying materially less equity volatility and drawdown for
    a similar level of spread income — the case for listing the contract.
    """
    import exchange.scenario as scenario_mod
    from sim.bots import BOT_PRESETS

    week = args.hedge_week
    try:
        scen = scenario_mod.load_week(week)
    except (OSError, ValueError):
        console.print(f"[red]No scenario file for week {week}[/red]")
        return
    if not scen.flag("futures_enabled"):
        console.print(f"[red]Week {week} does not list the index future[/red]")
        return

    async def run_arm(hedge_ratio: float) -> dict:
        scen_arm = scenario_mod.load_week(week)
        scen_arm.apply()
        try:
            shocks = scenario_mod.scenario_shocks(scen_arm, args.ticks)
            spec = [
                ("hedged_mm", {"team_id": "mm_under_test",
                               "hedge_ratio": hedge_ratio, "skew": 0.4}),
                ("aggressive_mm", {"team_id": "mm_rival"}),
                ("noise", {"team_id": "noise_1"}),
                ("noise", {"team_id": "noise_2"}),
                ("momentum", {"team_id": "momentum_1"}),
                ("control", {"team_id": "control"}),
            ]
            bots = make_population(spec, schedule=shocks, symbols=args.symbols)
            arena = HeadlessArena(bots, shocks=shocks, symbols=args.symbols,
                                  seed=args.seed, scenario=scen_arm,
                                  fire_shocks=False)
            try:
                await arena.open_session()
                await arena.run(args.ticks)
                await arena.close_session()
                summary = arena.summary()
                row = next(r for r in summary["bots"]
                           if r["team_id"] == "mm_under_test")
                curve = arena.equity["mm_under_test"]
                row["equity_vol"] = scoring.score_equity(curve)["vol"]
                row["max_drawdown"] = scoring.max_drawdown(curve)
                row["hedge_position"] = arena.server.portfolios[
                    "mm_under_test"].positions.get("ARENA10", 0)
                return row
            finally:
                arena.close()
        finally:
            scen_arm.restore()

    unhedged = await run_arm(0.0)
    hedged = await run_arm(args.hedge_ratio)

    console.print()
    console.rule("[bold]BROKER HEDGING — ARENA-10 index future[/bold]")
    console.print(f"  week {week}   seed {args.seed}   ticks {args.ticks:,}   "
                  f"hedge ratio {args.hedge_ratio}")
    table = Table(header_style="bold")
    table.add_column("arm")
    table.add_column("net worth", justify="right")
    table.add_column("spread capture", justify="right")
    table.add_column("rebates", justify="right")
    table.add_column("equity vol", justify="right")
    table.add_column("max drawdown", justify="right")
    table.add_column("fills", justify="right")
    table.add_column("index pos", justify="right")
    for name, r in (("unhedged", unhedged), ("hedged", hedged)):
        table.add_row(
            name, f"${r['net_worth']:,.0f}", f"${r['realized_pnl']:,.0f}",
            f"${r['rebates']:,.0f}", f"{r['equity_vol'] * 100:.4f}%",
            f"{r['max_drawdown'] * 100:.2f}%", f"{r['trades']:,}",
            f"{r['hedge_position']:+d}",
        )
    console.print(table)

    vol_cut = (1 - hedged["equity_vol"] / unhedged["equity_vol"]) \
        if unhedged["equity_vol"] else 0.0
    dd_cut = (1 - hedged["max_drawdown"] / unhedged["max_drawdown"]) \
        if unhedged["max_drawdown"] else 0.0
    console.print()
    console.print(f"  volatility reduction   [bold]{vol_cut:+.1%}[/bold]")
    console.print(f"  drawdown reduction     [bold]{dd_cut:+.1%}[/bold]")
    console.print(f"  net worth difference   "
                  f"[bold]${hedged['net_worth'] - unhedged['net_worth']:,.0f}"
                  f"[/bold]")
    console.print("  [dim]Season rank is risk-adjusted, so a hedge that cuts "
                  "vol without giving up much P&L is a scoring win even when "
                  "net worth is flat.[/dim]")


def _all_symbols() -> list[str]:
    import plugins.securities.defaults  # noqa: F401
    from plugins import arena as registry
    return [s.id for s in registry.list_securities()]


def parse_weeks(spec: str) -> list[int]:
    """'1-10' or '2,5,9' → [1..10] / [2,5,9]."""
    weeks: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            weeks.extend(range(int(a), int(b) + 1))
        elif part:
            weeks.append(int(part))
    return weeks


async def run_weeks(args: argparse.Namespace, console: Console) -> None:
    """Run the Phase 1 scenario files in sequence (one season per week)."""
    from exchange.scenario import load_week, scenario_shocks

    for week in parse_weeks(args.weeks):
        try:
            scen = load_week(week)
        except FileNotFoundError:
            console.print(f"[yellow]week {week}: no scenario file — skipped[/yellow]")
            continue
        # Each week runs under its own gate flags (config overrides via
        # apply(), order-entry gates via the arena's scenario).
        scen.apply()
        # Normally the week's own calendar supplies (and fires) the events.
        # With --shock-type the teacher is asking a different question — "what
        # does THIS shock do in week N?" — so the named shock is scheduled and
        # fired on top of whatever the week's calendar does.
        if args.shock_type:
            shocks = typed_schedule(args.shock_type, args.shocks, args.ticks)
            fire = True
        else:
            shocks = scenario_shocks(scen, args.ticks)
            fire = False
        console.print()
        console.rule(f"[bold cyan]WEEK {week:02d} — {scen.label}[/bold cyan]")
        console.print(f"  [dim]{scen.flag_summary()}[/dim]")
        await run_once(
            ticks=args.ticks, shocks=shocks, symbols=args.symbols,
            seed=(args.seed + week if args.seed is not None else None),
            insider=not args.no_insider, lead=args.lead, post=args.post,
            console=console, title=f"WEEK {week:02d} REPORT", scenario=scen,
            # The exchange's calendar engine fires the week's own events, with
            # ramps and dividends — exactly as it will in class.
            fire_shocks=fire,
            lineup=getattr(args, "lineup_spec", None),
            venues=args.venues, split_brain=args.split_brain,
        )
        scen.restore()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="AlgoArena season simulator / balance harness")
    ap.add_argument("--ticks", type=int, default=1500,
                    help="ticks per season (default 1500)")
    ap.add_argument("--shocks", type=int, default=3,
                    help="number of random shocks (ignored with --weeks)")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed for reproducible runs")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated subset of symbols to trade")
    ap.add_argument("--weeks", default=None,
                    help="run scenario files, e.g. '1-10' or '4,7,9'")
    ap.add_argument("--week", default=None,
                    help="friendlier alias for --weeks (e.g. --week 3)")
    ap.add_argument("--lineup", default=None, metavar="SPEC",
                    help="exact bot population, 'preset:count' comma-separated, "
                         "e.g. \"momentum:2,mean_reversion:1,passive_mm:1\". "
                         f"Presets: {', '.join(sorted(BOT_PRESETS))}. "
                         "Default: the standard balance-testing field.")
    ap.add_argument("--shock-type", default=None, metavar="ID",
                    help="fire this registered shock instead of random price "
                         "moves (combine with --shocks N). Ids: "
                         "earnings_beat, earnings_miss, flash_crash, "
                         "risk_on_rally, fed_rate_hike, fed_rate_cut, "
                         "sector_rotate, geo_crisis, vol_spike, "
                         "liquidity_crunch")
    ap.add_argument("--lead", type=int, default=20,
                    help="predictor entry lead / attribution window before (default 20)")
    ap.add_argument("--post", type=int, default=30,
                    help="attribution window after the shock (default 30)")
    ap.add_argument("--no-insider", action="store_true",
                    help="predictors know timing only, not direction")
    ap.add_argument("--compare-upgrade", default=None, metavar="KEY",
                    help="A/B one shop upgrade against its price "
                         "(position_limit, margin_plus, fee_tier, "
                         "order_quota, colocation)")
    ap.add_argument("--upgrade-target", default="mm_aggressive",
                    help="which bot receives the upgrade (default mm_aggressive)")
    ap.add_argument("--compare-hedge", action="store_true",
                    help="A/B a market maker that hedges with ARENA-10 "
                         "against one that does not")
    ap.add_argument("--hedge-ratio", type=float, default=1.0,
                    help="delta hedge ratio for --compare-hedge (default 1.0)")
    ap.add_argument("--hedge-week", type=int, default=9,
                    help="week scenario to run --compare-hedge under")
    ap.add_argument("--sessions-remaining", type=int, default=7,
                    help="sessions left after the purchase window, for the "
                         "ROI horizon (default 7 = weeks 4-10)")
    ap.add_argument("--venues", type=int, default=1, metavar="N",
                    help="run N exchanges side by side (default 1). Traders "
                         "split round-robin, brokers quote every venue from "
                         "one pricing brain; the report gains a venue "
                         "coherence line. Add 'arb:1' to --lineup to see an "
                         "arbitrageur close the gaps.")
    ap.add_argument("--split-brain", action="store_true",
                    help="market makers quote each venue off ITS OWN price "
                         "instead of one shared feed — the configuration in "
                         "which venues drift apart, and therefore the one "
                         "where an arbitrageur earns its keep")
    ap.add_argument("--quiet-log", action="store_true", default=True,
                    help="suppress exchange INFO logging (default on)")
    args = ap.parse_args()

    if args.quiet_log:
        import logging
        logging.getLogger("exchange.server").setLevel(logging.ERROR)
        logging.getLogger("plugins").setLevel(logging.ERROR)

    args.symbols = args.symbols.split(",") if args.symbols else None
    console = Console(width=max(160, Console().width))

    # --week is the friendly alias; --weeks wins if both are given.
    if args.week and not args.weeks:
        args.weeks = args.week

    # Lineup: validated up front so a typo fails before a season runs.
    args.lineup_spec = None
    if args.lineup:
        try:
            args.lineup_spec = parse_lineup(args.lineup)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(2)
        # A field with no market maker has no resting liquidity, so aggressive
        # bots trade nothing at all. Worth saying out loud rather than letting
        # the teacher read a report of zeroes.
        if not any(BOT_PRESETS[p].role == "broker"
                   for p, _ in args.lineup_spec):
            console.print("[yellow]Note: this lineup has no market maker "
                          "(passive_mm / aggressive_mm / hedged_mm), so the "
                          "book will be empty and almost nothing will trade. "
                          "Add e.g. 'passive_mm:1' for a realistic "
                          "matchup.[/yellow]")
        if not any(p == "control" for p, _ in args.lineup_spec):
            console.print("[dim]Tip: add 'control:1' for a do-nothing "
                          "benchmark to measure everyone against.[/dim]")

    if args.shock_type:
        valid = shock_ids()
        if args.shock_type not in valid:
            console.print(f"[red]Unknown shock {args.shock_type!r}. "
                          f"Choose from: {', '.join(valid)}[/red]")
            sys.exit(2)

    if args.seed is not None:
        random.seed(args.seed)

    if args.weeks:
        asyncio.run(run_weeks(args, console))
        return

    if args.compare_hedge:
        asyncio.run(compare_hedge(args, console))
        return

    if args.compare_upgrade:
        asyncio.run(compare_upgrade(
            args.compare_upgrade, args.upgrade_target, args, console))
        return

    # Symbols must be resolved before the schedule can pick targets, and the
    # arena is what knows the listing — build a throwaway to read it.
    symbols = args.symbols or _all_symbols()

    if args.shock_type:
        shocks = typed_schedule(args.shock_type, args.shocks, args.ticks)
    else:
        shocks = random_schedule(args.shocks, args.ticks, symbols)
    asyncio.run(run_once(
        ticks=args.ticks, shocks=shocks, symbols=args.symbols,
        seed=args.seed, insider=not args.no_insider,
        lead=args.lead, post=args.post, console=console,
        venues=args.venues, split_brain=args.split_brain,
        lineup=args.lineup_spec,
    ))


if __name__ == "__main__":
    main()
