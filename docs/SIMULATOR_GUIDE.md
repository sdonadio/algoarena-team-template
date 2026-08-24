# AlgoArena — Simulator Guide

Recipes for answering "who wins?" before you teach.

The simulator drives the **real** exchange — same matching engine, same
maker/taker fees, same margin, liquidation, quotas and week gates — with a
population of caricature bots on a virtual clock. A season takes about a
second, so you can settle an argument in the time it takes to type it.

Everything below is copy-paste. Nothing needs a network, a market-data feed,
or a running exchange.

```bash
make season                      # the default field, 3 random shocks
make whowins                     # same, with the verdict up top
make season-weeks WEEKS=1-10     # every week scenario in sequence
```

## How to read the report

Sections, in order:

| Section | The question it answers |
|---------|-------------------------|
| **WHO WINS** | Four one-line verdicts: most money, best risk-adjusted (the season ranking), best over the shock windows, best market maker — each with the margin over the runner-up |
| Final standings | Full table, net of fees, rebates and carry, versus a do-nothing control |
| Broker survival | Did the market makers stay solvent and keep quoting? |
| Exchange | Venue revenue = taker fees collected − maker rebates paid |
| Venue coherence | (multi-venue only) how far apart the venues' prices drifted |
| Shock attribution | Net-worth delta per bot in the window around each event |
| Order rejections | Which risk control actually bit, and how often |

Two habits worth keeping:

* **Always keep a `control` in the lineup.** Its return is the bar. A strategy
  that makes money in an up-drifting market has proved nothing.
* **One seed is one sample.** Re-run with `SEED=1 2 3` before you believe a
  close result. The report says so too.

## The knobs

| Flag / variable | Meaning |
|-----------------|---------|
| `--lineup "preset:count,…"` | The exact field. Presets: `passive_mm`, `aggressive_mm`, `hedged_mm`, `momentum`, `mean_reversion`, `noise`, `control`, `shock_predictor`, `arb` |
| `--shock-type ID` | Fire a real shock plugin instead of random moves. `flash_crash`, `earnings_beat`, `earnings_miss`, `risk_on_rally`, `fed_rate_hike`, `fed_rate_cut`, `sector_rotate`, `geo_crisis`, `vol_spike`, `liquidity_crunch` |
| `--shocks N` | How many shocks (of that type, if one is named) |
| `--week N` / `--weeks 1-10` | Run under a week scenario's rule set (same thing; `--week` is the friendly alias) |
| `--ticks N` | Season length (default 1500) |
| `--seed N` | Reproducibility |
| `--venues N` | Run N exchanges side by side |
| `--split-brain` | Market makers quote each venue off its own price instead of one shared feed |
| `--no-insider` | Shock predictors know timing only, not direction |
| `--compare-upgrade KEY` | A/B one shop upgrade against its price |
| `--compare-hedge` | A/B a market maker that hedges with ARENA-10 |

`make whowins` wires the common ones: `WEEK= LINEUP= SHOCK= SEED= TICKS= SHOCKS=`,
all optional.

---

## Recipe 1 — Who wins week 3 with my lineup?

```bash
make whowins WEEK=3 SEED=1 \
  LINEUP="momentum:2,mean_reversion:1,shock_predictor:1,passive_mm:1,aggressive_mm:1,noise:2,control:1"
```

Read the four WHO WINS lines. "Most money" and "Best risk-adjusted" often name
*different* bots — that is the point, because the season ranks on the
risk-adjusted score, so a bot that made slightly less with half the drawdown
wins the term. The margin on each line tells you whether the result is real or
noise; anything under a couple of percent, re-run with `SEED=2`.

Week 3 also turns on the earnings calendar and its position limit (600), so
compare with `WEEK=6` (limit 1000, dense calendar) to see how much of a
strategy's edge was really just size.

## Recipe 2 — Does a flash crash favour mean reversion or momentum?

```bash
make whowins SEED=1 SHOCK=flash_crash SHOCKS=3 \
  LINEUP="momentum:2,mean_reversion:2,passive_mm:1,aggressive_mm:1,noise:2,control:1"
```

Look at **Best on the shocks**, then the Shock attribution table below it,
which gives every bot's P&L in each event window separately. The mechanics to
point at: mean reversion buys the dislocation on the way down, momentum is on
the wrong side of the initial drop and only pays if the move keeps running —
and because AlgoArena shocks ramp over `SHOCK_RAMP_TICKS` with an overshoot,
both can be right in different windows.

Run it on three seeds (`SEED=1 2 3`). In practice the winner **changes between
seeds**, which is the honest lesson: over three crashes neither style has a
reliable edge at this fee level, and the market makers quietly out-earn both.
Then flip the sign — `SHOCK=risk_on_rally` — and compare. Also read the `fees`
column: momentum's IOC clips pay the taker fee every time, which is often the
whole difference.

## Recipe 3 — Does predicting shocks pay: timing, or direction?

```bash
make whowins SEED=3 SHOCKS=4 \
  LINEUP="shock_predictor:2,momentum:1,passive_mm:1,aggressive_mm:1,noise:2,control:1"

# and the honest version — timing only, no direction foreknowledge
PYTHONPATH=. python scripts/season_sim.py --seed 3 --shocks 4 --no-insider \
  --lineup "shock_predictor:2,momentum:1,passive_mm:1,aggressive_mm:1,noise:2,control:1"
```

The default field has both flavours side by side (`predictor_insider` and
`predictor_timing`); with a custom lineup use `--no-insider` to make every
predictor timing-only. Read the **"Does predicting shocks pay?"** block at the
bottom: it prints each predictor's total across every shock window next to the
average non-predictor.

What you want to see is timing+direction paying handsomely and timing-only
paying a little — because the in-game event calendar announces *timing* and
never direction. If timing-only earns nothing, the calendar is decoration; if
it earns a fortune, the calendar is the only strategy worth having.

## Recipe 4 — Is the fee_tier upgrade worth $160k?

```bash
PYTHONPATH=. python scripts/season_sim.py --compare-upgrade fee_tier \
  --upgrade-target mm_aggressive --seed 11 --sessions-remaining 7
```

Two runs on the same seed, identical except that one bot owns the upgrade. The
verdict line compares the gain over the remaining sessions against the catalog
price. Swap `--upgrade-target mm_passive` (a post-only maker cares about the
rebate, not the fee) or try `position_limit`, `margin_plus`, `order_quota`,
`colocation`.

Caveat printed by the tool and worth repeating in class: the sim bots cap their
own size, so a position-limit upgrade may not bind for them even though it
would for a student. Re-run across 2–3 seeds before you change a price.

## Recipe 5 — Does hedging with ARENA-10 help a market maker?

```bash
PYTHONPATH=. python scripts/season_sim.py --compare-hedge --hedge-week 9 \
  --hedge-ratio 1.0 --ticks 2000 --seed 3
```

Same seed, same flow, same quotes — the only difference is whether the desk
sells the index future against its equity book. Read the **volatility
reduction** and **drawdown reduction** lines, not the net-worth line: the case
for the contract is that it strips the beta out of a book the desk accumulated
just by doing its job, while keeping the spread income. Because the season
ranks risk-adjusted, cutting vol without giving up much P&L is a scoring win
even when net worth is flat.

**Run several seeds — the answer is not uniform.** On `--seed 5` and
`--seed 11` the hedge cuts vol by 18–35% and drawdown with it; on `--seed 1`
and `--seed 3` it makes both worse. Two reasons worth putting in front of the
class: the contract is marked to its index, not its own book, so it trades at
a basis the hedge does not capture, and the preset only rebalances every
`hedge_every` ticks, so between rebalances the hedge is the wrong size. A
delta hedge is not a free risk reduction — sizing and rebalancing frequency
are the whole game. Try `--hedge-ratio 0.5` for a partial hedge.

## Recipe 6 — Do multiple venues stay coherent with an arb bot?

```bash
# venues drifting on their own, no arbitrageur
PYTHONPATH=. python scripts/season_sim.py --venues 2 --split-brain --seed 1 \
  --lineup "passive_mm:1,aggressive_mm:1,momentum:2,mean_reversion:1,noise:2,control:1"

# same seed, same field, plus one arbitrageur
PYTHONPATH=. python scripts/season_sim.py --venues 2 --split-brain --seed 1 \
  --lineup "passive_mm:1,aggressive_mm:1,momentum:2,mean_reversion:1,noise:2,control:1,arb:1"
```

Compare the **Venue coherence** line. It reports the widest end-of-run gap
between two venues' mids for one symbol, the mean gap over the whole run, how
many arbs fired, and the average edge captured in bps.

Three things to notice:

1. Without an arbitrageur, `arbs 0` — nothing connects the venues, and the gap
   is whatever the two books happened to drift to.
2. With one, the gap narrows and every arb is logged with its captured edge.
   The edge is net of the taker fee on **both** legs, which is why a gap
   smaller than roughly twice the taker fee is left alone: closing it would
   lose money. Coherence is bounded by the fee schedule, not by good will —
   exactly as in a real fragmented market.
3. Drop `--split-brain` and the gap collapses on its own, because the market
   makers are then quoting every venue off one shared feed (what
   `broker/broker.py: _run_all_venues` does in class). That is the *first*
   line of defence; the arbitrageur is what handles the residual and what
   keeps venues honest when a broker's feed is stale.

In class, a cross-venue arbitrageur runs alongside the venues to pick up
that residual. Building one yourself is the Level 6 challenge (OPTION F in
`trader/trader.py`).

---

## Where the pieces live

| File | What it is |
|------|------------|
| `scripts/season_sim.py` | The CLI: schedules, lineups, A/B comparisons |
| `sim/arena.py` | The headless arena — real `ExchangeServer`(s), fake sockets, virtual clock |
| `sim/bots.py` | The presets. Deliberately simple caricatures, not student code |
| `sim/report.py` | The report, including the WHO WINS verdict |
| `tests/test_season_sim.py`, `tests/test_whowins.py`, `tests/test_multivenue.py` | What keeps all of the above honest |

Balance-testing checklist before a new week goes live:
[SEASON_GUIDE §10](SEASON_GUIDE.md).
