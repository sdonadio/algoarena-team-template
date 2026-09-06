"""
shared/envfile.py — minimal .env loader (stdlib only).

`make register` writes team credentials (ARENA_TOKEN, EXCHANGE_HOST, …) to
a gitignored `.env`. Make targets load it via `-include .env`, but students
who run bots directly (`TEAM_ID=x python -m team.trader`) used to need to
export it by hand first — forgetting that means AUTH_FAILED on connect.

load_env() closes that gap: it loads KEY=VALUE lines into os.environ
WITHOUT overriding variables already set in the shell, so explicit
`TEAM_ID=x python -m team.trader` still wins over the file. Called at
import time by the bot configs and the arena SDK.

No python-dotenv dependency — the format supported here is exactly what
create_team.py writes: comments, blank lines, KEY=VALUE (optional quotes).

Provenance
----------
Because load_env() copies file values INTO os.environ, a later reader cannot
tell "the student exported this in their shell" from "make register wrote
this into .env months ago". That distinction is the whole diagnosis when a
stale `EXCHANGE_HOST=<hosted arena IP>` sends a supposedly local bot to AWS,
so load_env() records every key it injected: see keys_from_env_file().
"""

from __future__ import annotations

import os
import pathlib

# Keys that load_env() copied out of a .env file into os.environ. A key the
# shell already defined is NOT recorded — the shell value won, so the shell
# is its source. Module-level on purpose: os.environ is process-global too.
_keys_from_file: set[str] = set()


def load_env(path: str | os.PathLike = ".env") -> bool:
    """Load KEY=VALUE lines from `path` into os.environ (shell vars win).

    Returns True if the file existed and was read, False otherwise.
    Never raises: a malformed line is skipped, an unreadable file ignored.

    Side effect (read it with keys_from_env_file()): every key this call
    injects is remembered, so callers can report where a value came from.
    """
    p = pathlib.Path(path)
    try:
        if not p.is_file():
            return False
        text = p.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            _keys_from_file.add(key)
    return True


def keys_from_env_file() -> frozenset[str]:
    """The env keys whose value came from a .env file, not from the shell."""
    return frozenset(_keys_from_file)


def came_from_env_file(key: str) -> bool:
    """True if `key`'s current os.environ value was injected from a .env file."""
    return key in _keys_from_file


def forget_env_file_keys() -> None:
    """Drop the provenance record (tests; a process re-reading a fresh .env)."""
    _keys_from_file.clear()
