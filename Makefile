# AlgoArena — student team Makefile
# The arena address and your token live in .env (created by make register).

-include .env
export

ARENA_URL ?= https://arena.example.edu
# The engine lives under engine/ — every target needs it importable.
export PYTHONPATH := engine:.
# The season's week scenarios (the rules and event calendar of each Friday
# block) ship under season/ so the season simulator runs offline.
export SEASON_DIR := season
export SEASON_PERSIST := false

install:
	pip install -r requirements.txt

register:
	python scripts/create_team.py --remote $(ARENA_URL)

# Prove your connection end-to-end (registration code needed: CODE=...)
test-remote:
	python scripts/test_remote.py --arena $(ARENA_URL) --code $(CODE)

# Run a bot seat: make trader BOT=<id>  — TEAM_ID=<id> works too.
TEAM_ID ?= $(BOT)

trader:
	TEAM_ID=$(TEAM_ID) python -m team.trader

broker:
	TEAM_ID=$(TEAM_ID) python -m team.broker

exchange:
	python -m exchange.server

sim:
	python tests/sim_session.py

# Simulate a graded week offline against the house bots: make season WEEK=3
WEEK ?= 1
season:
	python scripts/season_sim.py --weeks $(WEEK)

# Replay a recorded block into a local dashboard: make replay FILE=sessions/<file>
replay:
	python scripts/replay_session.py $(FILE) --speed 5

test:
	pytest tests/ -q

.PHONY: install register trader broker exchange sim season replay test
