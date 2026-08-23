# AlgoArena — Team Repository

Welcome to the arena.

## 0. Create your team's repo (once, one teammate does this)

If you are reading this on the **template repository**, don't clone it —
make your own copy first:

1. Click **Use this template → Create a new repository** (top right).
2. Name it after your team and make it **Private**.
3. Add your teammates and the teacher as collaborators
   (Settings → Collaborators).

Everyone then clones **your team's repo** — it's yours: only your team and
the teacher can see it. Never fork or push to the template itself.

## 1. Set up

```bash
pip install -r requirements.txt
```

## 2. Register your team

You start with a **$1,000,000 budget** to invest across an exchange license
($300k), broker desks ($100k+), and trader seats ($50k+). Your allocation
becomes each bot's starting cash. You'll need the class registration code
and the arena address from your teacher:

```bash
make register            # interactive wizard
```

This creates:
- `team/` — your starter code (this is where you work)
- `.env`  — your secret team token. **Never commit or share it** — anyone
  who has it can trade with your capital.

## 3. Run your bots

```bash
make trader BOT=<your_trader_id>     # ids are listed in team/README.md
make broker                          # if you bought a broker desk
```

Bots connect to the hosted arena, authenticate with your token, and wait
for the teacher to open the session. Watch yourself live on the class
dashboard.

## 4. Develop

- `team/trader.py` → `MyStrategy.generate_signal()` — your edge
- `team/broker.py` → quoting + inventory management (Level 4 keeps you solvent)
- Test offline, no network needed: `make sim` and `make test`

**Full connection guide (read this first): [docs/CONNECTING.md](docs/CONNECTING.md)**

Other guides: [QUICKSTART](docs/QUICKSTART.md) ·
[TRADER](docs/TRADER_GUIDE.md) · [BROKER](docs/BROKER_GUIDE.md) ·
[EXCHANGE](docs/EXCHANGE_GUIDE.md)

## Rules

- Commit early and often — your git history is part of your grade.
- Don't share code between teams. Repos are private for a reason.
- Don't commit `.env`.
