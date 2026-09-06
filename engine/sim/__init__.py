"""
sim/ — AlgoArena headless season simulator.

Drives the REAL exchange mechanics (matching, maker/taker fees, margin,
carry, liquidation, calendar) in-process with fake connections and a virtual
clock, so a whole season compresses into seconds. Used to answer balance
questions before students play — above all: does predicting shocks pay, and
by how much net of fees?

Entry point: scripts/season_sim.py
"""

from sim.arena import HeadlessArena, ScheduledShock, SimClient
from sim.bots import BOT_PRESETS, SimBot, make_population

__all__ = [
    "HeadlessArena",
    "ScheduledShock",
    "SimClient",
    "SimBot",
    "BOT_PRESETS",
    "make_population",
]
