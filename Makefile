# AlgoArena — student team Makefile
# The arena address and your token live in .env (created by make register).

-include .env
export

ARENA_URL ?= https://arena.example.edu
# The engine lives under engine/ — every target needs it importable.
export PYTHONPATH := engine:.

install:
	pip install -r requirements.txt

register:
	python scripts/create_team.py --remote $(ARENA_URL)

# Prove your connection end-to-end (registration code needed: CODE=...)
test-remote:
	python scripts/test_remote.py --arena $(ARENA_URL) --code $(CODE)

trader:
	TEAM_ID=$(BOT) python -m team.trader

broker:
	TEAM_ID=$(BOT) python -m team.broker

exchange:
	python -m exchange.server

sim:
	python tests/sim_session.py

test:
	pytest tests/ -q

.PHONY: install register trader broker exchange sim test
