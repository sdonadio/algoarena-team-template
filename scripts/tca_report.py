#!/usr/bin/env python
"""
scripts/tca_report.py — Transaction Cost Analysis for a recorded session.

Reads a session recording (sessions/session_*.jsonl, written by the exchange
between SESSION_OPEN and SESSION_CLOSED) and produces a per-team TCA report:
fill count, volume, buy/sell split, maker vs taker mix, fees vs rebates, and
execution quality measured against the prevailing mid at the moment of each
fill.

Usage:
    python scripts/tca_report.py sessions/session_20260820_141530.jsonl
    python scripts/tca_report.py <file> --out data/reports/
    python scripts/tca_report.py <file> --team trader_alpha
    python scripts/tca_report.py --latest

Output:
    <out>/<recording-stem>/<team_id>.md   one report per team
    <out>/<recording-stem>/summary.md     league table across teams
    plus a compact table on stdout.

METHODOLOGY
===========

Maker / taker
    TradeExecution.aggressor names the side that crossed the spread, so the
    taker is the buyer when aggressor == "buy" and the seller otherwise. The
    other side rested in the book and is the maker.

Fees
    With maker/taker pricing enabled (the default), TradeExecution.fee is paid
    in full by the taker and TradeExecution.maker_rebate is credited to the
    maker. Under the legacy flat model no rebate is paid and the fee is split
    50/50, which we detect by maker_rebate == 0 and attribute accordingly.

Prevailing mid
    The mid_price of the most recent BookSnapshot for that symbol seen *before*
    the trade line. Recordings are written in broadcast order, so this is the
    last mid every participant could have observed. Fills with no prior
    snapshot for the symbol are excluded from the execution-quality averages
    (they still count for volume and fees).

Execution quality, in basis points of the prevailing mid
    A buy is measured as (mid - price) / mid, a sell as (price - mid) / mid.
    Both are therefore *negative when the fill is worse than the mid*:
      - takers cross the spread, so their number is normally negative and is
        reported as SLIPPAGE (paying up = negative);
      - makers are filled on their resting quote, so their number is normally
        positive and is reported as SPREAD CAPTURE (earning the spread =
        positive).
    Per-team and per-symbol figures are share-volume-weighted averages of the
    per-fill numbers.
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if (ROOT / "engine").is_dir():     # student-template layout
    sys.path.insert(0, str(ROOT / "engine"))

from pydantic import ValidationError                              # noqa: E402
from rich.console import Console                                  # noqa: E402
from rich.table import Table                                      # noqa: E402

from shared.messages import (                                     # noqa: E402
    BookSnapshot,
    Leaderboard,
    TradeExecution,
    parse_message,
)

_console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Fill:
    """One side of one trade, seen from a single participant's point of view."""

    ts: float
    symbol: str
    side: str            # "buy" | "sell" — this participant's direction
    role: str            # "maker" | "taker"
    price: float
    quantity: int
    fee: float           # fee this participant paid on this fill
    rebate: float        # rebate this participant earned on this fill
    mid: float | None    # prevailing mid before the fill (None if unknown)

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    @property
    def quality_bps(self) -> float | None:
        """Signed execution quality vs the prevailing mid, in bps.

        Negative = filled worse than the mid (paid up), positive = filled
        better than the mid (earned the spread). See module docstring.
        """
        if not self.mid:
            return None
        edge = (self.mid - self.price) if self.side == "buy" else (self.price - self.mid)
        return edge / self.mid * 10_000.0


@dataclass
class Bucket:
    """Aggregated stats for a team, or for one symbol within a team."""

    fills: int = 0
    shares: int = 0
    notional: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    buy_shares: int = 0
    sell_shares: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    maker_shares: int = 0
    taker_shares: int = 0
    fees: float = 0.0
    rebates: float = 0.0
    # Volume-weighted accumulators for execution quality.
    _slip_num: float = 0.0      # taker fills
    _slip_den: int = 0
    _cap_num: float = 0.0       # maker fills
    _cap_den: int = 0

    def add(self, fill: Fill) -> None:
        self.fills += 1
        self.shares += fill.quantity
        self.notional += fill.notional
        if fill.side == "buy":
            self.buy_fills += 1
            self.buy_shares += fill.quantity
        else:
            self.sell_fills += 1
            self.sell_shares += fill.quantity
        if fill.role == "maker":
            self.maker_fills += 1
            self.maker_shares += fill.quantity
        else:
            self.taker_fills += 1
            self.taker_shares += fill.quantity
        self.fees += fill.fee
        self.rebates += fill.rebate

        bps = fill.quality_bps
        if bps is not None:
            if fill.role == "taker":
                self._slip_num += bps * fill.quantity
                self._slip_den += fill.quantity
            else:
                self._cap_num += bps * fill.quantity
                self._cap_den += fill.quantity

    # -- derived ------------------------------------------------------------

    @property
    def maker_ratio(self) -> float:
        return self.maker_fills / self.fills if self.fills else 0.0

    @property
    def net_fee_pnl(self) -> float:
        """Rebates earned minus fees paid (positive = the venue paid you)."""
        return self.rebates - self.fees

    @property
    def avg_slippage_bps(self) -> float | None:
        """Volume-weighted slippage over taker fills (negative = paid up)."""
        return self._slip_num / self._slip_den if self._slip_den else None

    @property
    def avg_capture_bps(self) -> float | None:
        """Volume-weighted spread capture over maker fills (positive = earned)."""
        return self._cap_num / self._cap_den if self._cap_den else None

    @property
    def fee_bps(self) -> float | None:
        """Net fee P&L as bps of traded notional."""
        return self.net_fee_pnl / self.notional * 10_000.0 if self.notional else None


@dataclass
class TeamTCA:
    team_id: str
    total: Bucket = field(default_factory=Bucket)
    by_symbol: dict[str, Bucket] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    leaderboard: dict | None = None      # final leaderboard entry, if any

    def add(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.total.add(fill)
        self.by_symbol.setdefault(fill.symbol, Bucket()).add(fill)


# ─────────────────────────────────────────────────────────────────────────────
# Recording ingest
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Recording:
    path: pathlib.Path
    teams: dict[str, TeamTCA]
    trades: int
    snapshots: int
    skipped: int
    exchange_fees: float
    first_ts: float
    last_ts: float


def _mid_of(snap: BookSnapshot) -> float | None:
    """Prevailing mid for a snapshot — mid_price, or derived from the book."""
    if snap.mid_price > 0:
        return snap.mid_price
    bid = snap.bids[0][0] if snap.bids else None
    ask = snap.asks[0][0] if snap.asks else None
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask or None


def _split_fees(trade: TradeExecution) -> tuple[float, float, float, float]:
    """(taker_fee, taker_rebate, maker_fee, maker_rebate) for one trade.

    Maker/taker model → the whole fee lands on the taker and the rebate on the
    maker. Legacy flat model (no rebate) → fee split 50/50.
    """
    if trade.maker_rebate:
        return trade.fee, 0.0, 0.0, trade.maker_rebate
    half = trade.fee / 2.0
    return half, 0.0, half, 0.0


def load_recording(path: str | pathlib.Path) -> Recording:
    """Parse a JSONL recording into per-team TCA aggregates.

    Malformed lines, unknown message types and schema violations are skipped
    (counted in Recording.skipped) so a truncated recording still reports.
    """
    path = pathlib.Path(path)
    teams: dict[str, TeamTCA] = {}
    mids: dict[str, float] = {}
    final_lb: Leaderboard | None = None
    trades = snapshots = skipped = 0
    first_ts = last_ts = 0.0

    def team(team_id: str) -> TeamTCA:
        return teams.setdefault(team_id, TeamTCA(team_id=team_id))

    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = float(rec["ts"])
                msg = parse_message(rec["msg"])
            except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
                skipped += 1
                continue

            first_ts = first_ts or ts
            last_ts = ts

            if isinstance(msg, BookSnapshot):
                snapshots += 1
                mid = _mid_of(msg)
                if mid:
                    mids[msg.symbol] = mid

            elif isinstance(msg, TradeExecution):
                trades += 1
                mid = mids.get(msg.symbol)
                taker_id = msg.buyer_id if msg.aggressor == "buy" else msg.seller_id
                t_fee, t_reb, m_fee, m_reb = _split_fees(msg)

                for party, side in ((msg.buyer_id, "buy"), (msg.seller_id, "sell")):
                    is_taker = party == taker_id
                    team(party).add(Fill(
                        ts=ts,
                        symbol=msg.symbol,
                        side=side,
                        role="taker" if is_taker else "maker",
                        price=msg.price,
                        quantity=msg.quantity,
                        fee=t_fee if is_taker else m_fee,
                        rebate=t_reb if is_taker else m_reb,
                        mid=mid,
                    ))

            elif isinstance(msg, Leaderboard):
                final_lb = msg

    exchange_fees = final_lb.exchange_fees if final_lb else 0.0
    if final_lb:
        for entry in list(final_lb.traders) + list(final_lb.brokers):
            tid = entry.get("team_id")
            if tid in teams:
                teams[tid].leaderboard = entry

    return Recording(
        path=path,
        teams=teams,
        trades=trades,
        snapshots=snapshots,
        skipped=skipped,
        exchange_fees=exchange_fees,
        first_ts=first_ts,
        last_ts=last_ts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bps(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _signed_money(value: float) -> str:
    return f"{'-' if value < 0 else '+'}${abs(value):,.2f}"


def _positions(raw: object) -> str:
    if not isinstance(raw, dict) or not raw:
        return "flat"
    return ", ".join(f"{sym} {qty:+d}" for sym, qty in sorted(raw.items()))


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_team_markdown(rec: Recording, team: TeamTCA) -> str:
    t = team.total
    span_min = (rec.last_ts - rec.first_ts) / 60.0
    lines: list[str] = [
        f"# TCA — {team.team_id}",
        "",
        f"Recording: `{rec.path.name}`  ",
        f"Session length: {span_min:.1f} min · {rec.trades} trades in the tape",
        "",
        "## Execution summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Fills | {t.fills} |",
        f"| Volume (shares) | {t.shares:,} |",
        f"| Volume (notional) | ${_money(t.notional)} |",
        f"| Buys / Sells (fills) | {t.buy_fills} / {t.sell_fills} |",
        f"| Buys / Sells (shares) | {t.buy_shares:,} / {t.sell_shares:,} |",
        f"| Maker / Taker fills | {t.maker_fills} / {t.taker_fills} |",
        f"| Maker ratio | {t.maker_ratio * 100:.1f}% |",
        f"| Avg fill size | {t.shares / t.fills if t.fills else 0.0:.1f} shares |",
        "",
        "## Fees",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Fees paid | ${_money(t.fees)} |",
        f"| Rebates earned | ${_money(t.rebates)} |",
        f"| **Net fee P&L** | **{_signed_money(t.net_fee_pnl)}** |",
        f"| Net fee P&L (bps of notional) | {_bps(t.fee_bps)} |",
        "",
        "## Execution quality vs prevailing mid",
        "",
        "Signed in bps of the mid: negative = filled worse than the mid.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Taker slippage (vol-weighted) | {_bps(t.avg_slippage_bps)} bps "
        f"over {t.taker_shares:,} shares |",
        f"| Maker spread capture (vol-weighted) | {_bps(t.avg_capture_bps)} bps "
        f"over {t.maker_shares:,} shares |",
        "",
        "## Per-symbol breakdown",
        "",
        "| Symbol | Fills | Shares | Notional | Maker % | Fees | Rebates "
        "| Net fee P&L | Slippage bps | Capture bps |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for sym, b in sorted(team.by_symbol.items(), key=lambda kv: -kv[1].notional):
        lines.append(
            f"| {sym} | {b.fills} | {b.shares:,} | ${_money(b.notional)} "
            f"| {b.maker_ratio * 100:.0f}% | ${_money(b.fees)} | ${_money(b.rebates)} "
            f"| {_signed_money(b.net_fee_pnl)} | {_bps(b.avg_slippage_bps)} "
            f"| {_bps(b.avg_capture_bps)} |"
        )

    if team.leaderboard:
        e = team.leaderboard
        lines += [
            "",
            "## Final leaderboard entry",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Role | {e.get('role', '—')} |",
            f"| Net worth | ${_money(float(e.get('net_worth', 0.0)))} |",
            f"| Cash | ${_money(float(e.get('cash', 0.0)))} |",
            f"| Realized P&L | {_signed_money(float(e.get('realized_pnl', 0.0)))} |",
            f"| Unrealized P&L | {_signed_money(float(e.get('unrealized_pnl', 0.0)))} |",
            f"| Carry paid | ${_money(float(e.get('total_carry_paid', 0.0)))} |",
            f"| Liquidated | {'yes' if e.get('liquidated') else 'no'} |",
            f"| Open positions | {_positions(e.get('positions'))} |",
        ]
    else:
        lines += ["", "_No final leaderboard entry found in this recording._"]

    lines += [
        "",
        "---",
        "",
        "Method: taker = the aggressor side of the trade; the resting side is "
        "the maker. Prevailing mid = the last BookSnapshot mid for that symbol "
        "broadcast before the fill. Buys are scored (mid − price) / mid and "
        "sells (price − mid) / mid, share-volume weighted.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_summary_markdown(rec: Recording, teams: list[TeamTCA]) -> str:
    by_fee = sorted(teams, key=lambda t: -t.total.net_fee_pnl)
    by_slip = sorted(
        [t for t in teams if t.total.avg_slippage_bps is not None],
        key=lambda t: -(t.total.avg_slippage_bps or 0.0),
    )
    span_min = (rec.last_ts - rec.first_ts) / 60.0

    lines = [
        f"# TCA summary — `{rec.path.name}`",
        "",
        f"- Participants with fills: {len(teams)}",
        f"- Trades in tape: {rec.trades}",
        f"- Book snapshots: {rec.snapshots}",
        f"- Session length: {span_min:.1f} min",
        f"- Exchange fees collected (final leaderboard): ${_money(rec.exchange_fees)}",
        f"- Malformed lines skipped: {rec.skipped}",
        "",
        "## Ranked by net fee P&L (rebates − fees)",
        "",
        "| # | Team | Fills | Shares | Notional | Maker % | Fees | Rebates "
        "| Net fee P&L |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for i, t in enumerate(by_fee, 1):
        b = t.total
        lines.append(
            f"| {i} | {t.team_id} | {b.fills} | {b.shares:,} | ${_money(b.notional)} "
            f"| {b.maker_ratio * 100:.0f}% | ${_money(b.fees)} | ${_money(b.rebates)} "
            f"| {_signed_money(b.net_fee_pnl)} |"
        )

    lines += [
        "",
        "## Ranked by taker slippage (least negative first)",
        "",
        "| # | Team | Taker fills | Taker shares | Slippage bps | Capture bps |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for i, t in enumerate(by_slip, 1):
        b = t.total
        lines.append(
            f"| {i} | {t.team_id} | {b.taker_fills} | {b.taker_shares:,} "
            f"| {_bps(b.avg_slippage_bps)} | {_bps(b.avg_capture_bps)} |"
        )
    if not by_slip:
        lines.append("| — | _no taker fills with a prevailing mid_ | | | | |")

    lines += ["", "Per-team detail: see the sibling `<team_id>.md` files.", ""]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Terminal output
# ─────────────────────────────────────────────────────────────────────────────

def print_table(rec: Recording, teams: list[TeamTCA]) -> None:
    table = Table(
        title=f"TCA — {rec.path.name}  ({rec.trades} trades, "
              f"{(rec.last_ts - rec.first_ts) / 60:.1f} min)",
        show_lines=False,
    )
    table.add_column("Team", style="bold", no_wrap=True)
    table.add_column("Fills", justify="right")
    table.add_column("Notional", justify="right")
    table.add_column("Mkr%", justify="right")
    table.add_column("Fees", justify="right")
    table.add_column("Rebates", justify="right")
    table.add_column("Net fee", justify="right")
    table.add_column("Slip bps", justify="right")
    table.add_column("Cap bps", justify="right")

    for t in sorted(teams, key=lambda x: -x.total.net_fee_pnl):
        b = t.total
        net_c = "green" if b.net_fee_pnl >= 0 else "red"
        table.add_row(
            t.team_id,
            str(b.fills),
            f"${b.notional:,.0f}",
            f"{b.maker_ratio * 100:.0f}%",
            f"{b.fees:,.2f}",
            f"{b.rebates:,.2f}",
            f"[{net_c}]{b.net_fee_pnl:+,.2f}[/{net_c}]",
            _bps(b.avg_slippage_bps),
            _bps(b.avg_capture_bps),
        )
    _console.print(table)
    if rec.skipped:
        _console.print(f"[yellow]{rec.skipped} malformed line(s) skipped[/yellow]")


# ─────────────────────────────────────────────────────────────────────────────
# Report writing
# ─────────────────────────────────────────────────────────────────────────────

def write_reports(
    rec: Recording,
    teams: list[TeamTCA],
    out_base: pathlib.Path,
    summary: bool = True,
) -> list[pathlib.Path]:
    """Write <out_base>/<stem>/<team>.md for each team, plus summary.md.

    summary=False skips the league table — used with --team so a single-team
    run never overwrites the full-session summary.
    """
    out_dir = out_base / rec.path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []

    for team in teams:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in team.team_id)
        path = out_dir / f"{safe}.md"
        path.write_text(render_team_markdown(rec, team))
        written.append(path)

    if summary:
        path = out_dir / "summary.md"
        path.write_text(render_summary_markdown(rec, teams))
        written.append(path)
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Transaction Cost Analysis for a recorded AlgoArena session"
    )
    ap.add_argument("file", nargs="?", help="sessions/session_*.jsonl")
    ap.add_argument("--latest", action="store_true",
                    help="analyse the newest recording in sessions/")
    ap.add_argument("--out", default=None,
                    help="output directory (default: data/reports/)")
    ap.add_argument("--team", default=None,
                    help="only report this team id")
    ap.add_argument("--no-write", action="store_true",
                    help="print the table only, write no files")
    args = ap.parse_args(argv)

    path = args.file
    if args.latest or not path:
        candidates = sorted(glob.glob(str(ROOT / "sessions" / "session_*.jsonl")))
        if not candidates:
            print("No recordings found in sessions/ — run a session first",
                  file=sys.stderr)
            return 2
        path = candidates[-1]
        print(f"  Using latest recording: {path}")

    if not pathlib.Path(path).exists():
        print(f"No such recording: {path}", file=sys.stderr)
        return 2

    rec = load_recording(path)
    if not rec.teams:
        print(f"No trades found in {path} — nothing to analyse", file=sys.stderr)
        return 1

    teams = list(rec.teams.values())
    if args.team:
        teams = [t for t in teams if t.team_id == args.team]
        if not teams:
            known = ", ".join(sorted(rec.teams)) or "(none)"
            print(f"Team {args.team!r} has no fills in this recording. "
                  f"Teams with fills: {known}", file=sys.stderr)
            return 1

    print_table(rec, teams)

    if not args.no_write:
        out_base = pathlib.Path(args.out) if args.out else ROOT / "data" / "reports"
        written = write_reports(rec, teams, out_base, summary=not args.team)
        _console.print(
            f"  Wrote {len(written)} file(s) to "
            f"[bold]{(out_base / rec.path.stem)}[/bold]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
