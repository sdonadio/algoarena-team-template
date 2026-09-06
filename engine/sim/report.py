"""
sim/report.py — season report rendering (rich tables).

The report exists to answer four balance questions:

  1. Standings — who made money, and was it net of fees?
  2. Broker survival — did the market makers stay solvent and keep quoting?
  3. Exchange revenue — is running a venue worth the licence fee?
  4. Shock attribution — does predicting shocks pay, and by how much?

(4) is the one that decides whether the event calendar is good game design:
if foreknowledge is worth less than the round-trip cost, nobody will use the
calendar; if it is worth a fortune, the calendar becomes the only strategy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from sim.arena import HeadlessArena


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _pct(x: float) -> str:
    return f"{x:+.2%}"


def _ret(row: dict) -> float:
    """Return measured from post-grant opening equity, not starting cash."""
    base = row.get("start_equity") or row.get("starting_cash") or 0.0
    return (row["net_worth"] / base - 1.0) if base else 0.0


def render(
    arena: "HeadlessArena",
    console: Console | None = None,
    lead: int = 20,
    post: int = 30,
    title: str = "SEASON REPORT",
) -> dict[str, Any]:
    """Print the full season report. Returns the summary dict it rendered."""
    console = console or Console()
    s = arena.summary()

    console.print()
    console.rule(f"[bold]{title}[/bold]")
    console.print(
        f"  ticks [bold]{s['ticks']:,}[/bold]   "
        f"fills [bold]{s['trades']:,}[/bold]   "
        f"shocks [bold]{len(s['shocks'])}[/bold]   "
        f"seed [bold]{s['seed']}[/bold]"
    )

    _who_wins(console, arena, s, lead, post)
    _standings(console, s)
    _brokers(console, s)
    _exchange(console, s)
    _price_quality(console, s)
    _coherence(console, s)
    _attribution(console, arena, s, lead, post)
    _rejects(console, s)
    return s


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _who_wins(console: Console, arena: "HeadlessArena", s: dict,
              lead: int, post: int) -> dict[str, Any]:
    """The verdict, in four lines a teacher can read while talking.

    Four different questions, because "who wins" is genuinely ambiguous:
    biggest pile of money, best money-per-unit-of-risk (which is how the
    season is actually scored), best reaction to the events, and best market
    maker. Each line carries the margin over the runner-up, because a 0.1%
    win on one seed is not a result.
    """
    import exchange.scoring as scoring

    rows = [r for r in s["bots"] if r["preset"] != "control"]
    if not rows:
        return {}

    def podium(scored: list[tuple[dict, float]]) -> tuple[dict, float, float] | None:
        """(winner row, its score, margin over the runner-up)."""
        if not scored:
            return None
        ordered = sorted(scored, key=lambda kv: kv[1], reverse=True)
        best, best_score = ordered[0]
        margin = best_score - ordered[1][1] if len(ordered) > 1 else best_score
        return best, best_score, margin

    console.print()
    console.rule("[bold yellow]WHO WINS[/bold yellow]")

    verdict: dict[str, Any] = {}

    # 1. Most money.
    got = podium([(r, r["net_worth"]) for r in rows])
    if got:
        r, score, margin = got
        verdict["net_worth"] = r["team_id"]
        console.print(
            f"  [bold]Most money[/bold]          "
            f"[bold green]{r['team_id']}[/bold green] ({r['preset']}) "
            f"ended on {_money(score)}, {_money(margin)} ahead of the next bot "
            f"— {_pct(_ret(r))} return.")

    # 2. Risk-adjusted — the way the season is actually scored.
    scored = []
    for r in rows:
        curve = arena.equity.get(r["team_id"]) or []
        metrics = scoring.score_equity(curve)
        scored.append((r, metrics["risk_adjusted"], metrics))
    got = podium([(r, sc) for r, sc, _ in scored])
    if got:
        r, score, margin = got
        metrics = next(m for rr, _, m in scored if rr is r)
        verdict["risk_adjusted"] = r["team_id"]
        console.print(
            f"  [bold]Best risk-adjusted[/bold]  "
            f"[bold green]{r['team_id']}[/bold green] ({r['preset']}) "
            f"scores {score:.3f} vs {score - margin:.3f} for the runner-up "
            f"— max drawdown {metrics['max_drawdown']:.1%}. "
            f"[dim]This is the season ranking.[/dim]")

    # 3. Shock windows — who actually traded the news.
    shocks = s.get("shocks") or []
    if shocks:
        def window_total(team_id: str) -> float:
            return sum(arena.window_pnl(team_id, sh.tick - lead, sh.tick + post)
                       for sh in shocks)
        got = podium([(r, window_total(r["team_id"])) for r in rows])
        if got:
            r, score, margin = got
            verdict["shock_window"] = r["team_id"]
            console.print(
                f"  [bold]Best on the shocks[/bold]  "
                f"[bold green]{r['team_id']}[/bold green] ({r['preset']}) "
                f"made {_money(score)} over {len(shocks)} shock windows, "
                f"{_money(margin)} better than the next bot.")

    # 4. Best market maker — a different job, judged separately.
    brokers = [r for r in rows if r["role"] == "broker"]
    got = podium([(r, r["net_worth"]) for r in brokers])
    if got:
        r, score, margin = got
        verdict["broker"] = r["team_id"]
        alive = "survived" if not r["liquidated"] else "[red]was LIQUIDATED[/red]"
        maker = (100.0 * r["maker_count"] / r["trades"]) if r["trades"] else 0.0
        extra = (f", {_money(margin)} ahead of the next desk"
                 if len(brokers) > 1 else " (only desk in the field)")
        console.print(
            f"  [bold]Best market maker[/bold]   "
            f"[bold green]{r['team_id']}[/bold green] ({r['preset']}) "
            f"{alive} on {_money(score)}{extra} — {maker:.0f}% of its fills "
            f"were passive.")

    control = next((r for r in s["bots"] if r["preset"] == "control"), None)
    if control:
        beat = sum(1 for r in rows if _ret(r) > _ret(control))
        console.print(
            f"  [dim]{beat} of {len(rows)} bots beat doing nothing "
            f"({_pct(_ret(control))}). Anything below that line lost to a "
            f"buy-and-hold control.[/dim]")
    console.print("  [dim]One seed is one sample — re-run with 2-3 seeds "
                  "before believing a close result.[/dim]")
    return verdict


def _standings(console: Console, s: dict) -> None:
    t = Table(title="Final standings", title_justify="left",
              header_style="bold", expand=False)
    t.add_column("bot")
    t.add_column("preset")
    t.add_column("net worth", justify="right")
    t.add_column("return", justify="right")
    t.add_column("realized", justify="right")
    t.add_column("unreal", justify="right")
    t.add_column("fees", justify="right")
    t.add_column("rebates", justify="right")
    t.add_column("carry", justify="right")
    t.add_column("fills", justify="right")
    t.add_column("maker%", justify="right")

    rows = sorted(s["bots"], key=lambda r: r["net_worth"], reverse=True)
    control = next((r for r in rows if r["preset"] == "control"), None)
    bench = _ret(control) if control else 0.0

    for r in rows:
        ret = _ret(r)
        colour = "green" if ret > bench else "red"
        name = r["team_id"] + (" [red]LIQ[/red]" if r["liquidated"] else "")
        maker = (100.0 * r["maker_count"] / r["trades"]) if r["trades"] else 0.0
        t.add_row(
            name, r["preset"], _money(r["net_worth"]),
            f"[{colour}]{_pct(ret)}[/{colour}]",
            _money(r["realized_pnl"]), _money(r["unrealized_pnl"]),
            _money(r["fees"]), _money(r["rebates"]), _money(r["carry"]),
            f"{r['trades']:,}", f"{maker:.0f}%",
        )
    console.print()
    console.print(t)
    if control:
        console.print(f"  [dim]control (buy-and-hold) benchmark: "
                      f"{_pct(bench)} — beating this is the bar[/dim]")


def _brokers(console: Console, s: dict) -> None:
    brokers = [r for r in s["bots"] if r["role"] == "broker"]
    if not brokers:
        return
    t = Table(title="Broker survival", title_justify="left",
              header_style="bold")
    t.add_column("broker")
    t.add_column("status")
    t.add_column("net worth", justify="right")
    t.add_column("spread capture", justify="right")
    t.add_column("rebates", justify="right")
    t.add_column("fees", justify="right")
    t.add_column("carry", justify="right")
    t.add_column("maker%", justify="right")
    for r in brokers:
        status = "[red]LIQUIDATED[/red]" if r["liquidated"] else "[green]alive[/green]"
        maker = (100.0 * r["maker_count"] / r["trades"]) if r["trades"] else 0.0
        t.add_row(
            r["team_id"], status, _money(r["net_worth"]),
            _money(r["realized_pnl"]), _money(r["rebates"]),
            _money(r["fees"]), _money(r["carry"]), f"{maker:.0f}%",
        )
    console.print()
    console.print(t)


def _exchange(console: Console, s: dict) -> None:
    total_fees = sum(r["fees"] for r in s["bots"])
    total_rebates = sum(r["rebates"] for r in s["bots"])
    console.print()
    console.print(
        f"[bold]Exchange[/bold]  revenue {_money(s['exchange_revenue'])}  "
        f"(taker fees {_money(total_fees)} − maker rebates "
        f"{_money(total_rebates)})"
    )


def _price_quality(console: Console, s: dict) -> None:
    """Does the price move like a stock, or does it teleport?

    Measured OUTSIDE event windows, because shocks are meant to be violent.
    A real large-cap moves ~0.01-0.02% per half-second; anything with a p95
    in tenths of a percent is noise injection, not price discovery. A median
    of exactly zero is the opposite failure — a market nobody can trade.
    """
    pq = s.get("price_quality")
    if not pq or not pq.get("samples"):
        return
    p95, mx = pq["p95"], pq["max"]
    colour = "green" if p95 < 0.0015 else ("yellow" if p95 < 0.005 else "red")
    mcol = "green" if mx < 0.004 else ("yellow" if mx < 0.02 else "red")
    worst = pq["max_symbol"] or "—"
    console.print()
    console.print(
        f"[bold]Price quality[/bold]  tick-to-tick |move| outside event "
        f"windows:  median {pq['median']:.3%}   "
        f"p95 [{colour}]{p95:.3%}[/{colour}]   "
        f"max [{mcol}]{mx:.3%}[/{mcol}] ({worst} @ t{pq['max_tick']})   "
        f"[dim]{pq['samples']:,} samples, {pq['windows']} event windows "
        f"excluded[/dim]"
    )
    if pq["median"] <= 0.0:
        console.print("  [yellow]median move is zero — the price is frozen "
                      "between events, which is as broken as teleporting"
                      "[/yellow]")


def _coherence(console: Console, s: dict) -> None:
    """Do fragmented venues stay in line? (multi-venue runs only)

    The number that matters is the widest end-of-run gap between two venues'
    mids for one symbol. Real fragmented markets sit inside a spread or two
    because arbitrageurs close anything wider; an AlgoArena with no
    arbitrageur was observed drifting to 70%.
    """
    coh = s.get("coherence")
    if not coh:
        return
    gap = coh["max_gap"]
    colour = "green" if gap < 0.01 else ("yellow" if gap < 0.05 else "red")
    worst = coh["max_gap_symbol"] or "—"
    console.print()
    console.print(
        f"[bold]Venue coherence[/bold]  {coh['venues']} venues   "
        f"max cross-venue mid gap [{colour}]{gap:.2%}[/{colour}] ({worst})   "
        f"mean gap over the run {coh['mean_gap']:.2%}   "
        f"arbs {coh['arbs']:,}   "
        f"avg captured edge {coh['avg_edge_bps']:.1f} bps"
    )
    if coh["arbs"] == 0:
        console.print("  [dim]no arbitrage happened — add 'arb:1' to the "
                      "lineup and compare the gap[/dim]")


def _attribution(console: Console, arena: "HeadlessArena", s: dict,
                 lead: int, post: int) -> None:
    """Net-worth delta per bot summed over every shock window."""
    shocks = s["shocks"]
    if not shocks:
        return

    t = Table(
        title=f"Shock attribution — net worth delta over "
              f"[shock-{lead}, shock+{post}]",
        title_justify="left", header_style="bold",
    )
    t.add_column("bot")
    t.add_column("preset")
    for sh in shocks:
        t.add_column(sh.describe(), justify="right")
    t.add_column("total", justify="right", style="bold")

    rows = sorted(s["bots"], key=lambda r: r["team_id"])
    for r in rows:
        cells, total = [], 0.0
        for sh in shocks:
            d = arena.window_pnl(r["team_id"], sh.tick - lead, sh.tick + post)
            total += d
            colour = "green" if d > 0 else ("red" if d < 0 else "dim")
            cells.append(f"[{colour}]{_money(d)}[/{colour}]")
        colour = "green" if total > 0 else ("red" if total < 0 else "dim")
        t.add_row(r["team_id"], r["preset"], *cells,
                  f"[{colour}]{_money(total)}[/{colour}]")
    console.print()
    console.print(t)

    # The headline comparison: insider vs timing-only vs everyone else.
    def _tot(team_id: str) -> float:
        return sum(arena.window_pnl(team_id, sh.tick - lead, sh.tick + post)
                   for sh in shocks)

    preds = [r for r in s["bots"] if r["preset"] == "shock_predictor"]
    others = [r for r in s["bots"] if r["preset"] not in
              ("shock_predictor", "control")]
    if preds:
        console.print()
        console.print("  [bold]Does predicting shocks pay?[/bold]")
        console.print("  [dim]window deltas are already net of fees, "
                      "rebates and carry paid inside the window[/dim]")
        for r in preds:
            edge = _tot(r["team_id"])
            insider = r.get("insider")
            if insider is None:                 # older summaries: fall back
                insider = "insider" in r["team_id"]
            kind = "timing+direction" if insider else "timing only"
            console.print(
                f"    {r['team_id']:<22} {kind:<18} {_money(edge):>14}   "
                f"(season fees {_money(r['fees'])})"
            )
        if others:
            avg = sum(_tot(r["team_id"]) for r in others) / len(others)
            console.print(f"    {'(avg non-predictor)':<22} {_money(avg):>14}")


def _rejects(console: Console, s: dict) -> None:
    """Rejection codes — how often each risk control actually bit."""
    agg: dict[str, int] = {}
    for r in s["bots"]:
        for code, n in r["rejects"].items():
            agg[code] = agg.get(code, 0) + n
    if not agg:
        return
    console.print()
    console.print("[bold]Order rejections[/bold] (risk controls that bit)")
    for code, n in sorted(agg.items(), key=lambda kv: -kv[1]):
        console.print(f"    {code:<22} {n:>10,}")

    broken = [(r["team_id"], r["bot_errors"]) for r in s["bots"]
              if r["bot_errors"]]
    if broken:
        console.print("[yellow]  bot exceptions:[/yellow]")
        for team, errs in broken:
            console.print(f"    {team}: {errs[0]}")
