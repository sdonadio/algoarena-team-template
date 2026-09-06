"""
tests/sim_session.py — compatibility shim for sim/session.py.

The end-to-end, no-network simulation now lives in **sim/session.py**. Import
it from there:

    from sim.session import SimSession

Why it moved
------------
`tests/` has no `__init__.py`, so it is a namespace package, and any
third-party regular `tests` package installed in site-packages (several ML
libraries ship one) shadows it regardless of sys.path order. That made
`from tests.sim_session import SimSession` fail on a large fraction of student
machines. `sim/` is a real package, so `sim.session` cannot be shadowed.

Nothing was dropped. This module re-exports every public name the old one
exposed, so all three historical import forms still work and all three yield
the SAME class objects:

    from sim.session import SimSession                        # preferred
    from tests.sim_session import SimSession                  # still works
    sys.path.insert(0, "tests"); from sim_session import …    # still works

The script entry point is unchanged too:

    python tests/sim_session.py        # == make sim
    python -m sim.session              # same run

CLAUDE.md rule 5 ("sim_session.py must run without any network connections")
still holds — see the docstring in sim/session.py.
"""

from __future__ import annotations

# Run as a script (`python tests/sim_session.py`) sys.path[0] is tests/, not
# the repo root, so `import sim.session` would fail. Put the root on the path
# first. In the student template the engine lives under engine/ — make that
# importable too.
import pathlib as _pathlib
import sys as _sys

_root = _pathlib.Path(__file__).resolve().parent.parent
_engine = _root / "engine"
if str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))
if _engine.is_dir() and str(_engine) not in _sys.path:
    _sys.path.insert(0, str(_engine))
del _pathlib, _sys, _root, _engine

from sim.session import (  # noqa: E402,F401  (re-export)
    SessionResult,
    SignalFn,
    SimSession,
    SimulatedBroker,
    SimulatedExchange,
    SimulatedTrader,
    main,
)
from sim.session import _ascii_chart  # noqa: E402,F401  (used by the labs)

__all__ = [
    "SimSession",
    "SessionResult",
    "SimulatedExchange",
    "SimulatedBroker",
    "SimulatedTrader",
    "SignalFn",
    "main",
]


if __name__ == "__main__":
    main()
