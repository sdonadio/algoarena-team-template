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
"""

from __future__ import annotations

import os
import pathlib


def load_env(path: str | os.PathLike = ".env") -> bool:
    """Load KEY=VALUE lines from `path` into os.environ (shell vars win).

    Returns True if the file existed and was read, False otherwise.
    Never raises: a malformed line is skipped, an unreadable file ignored.
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
    return True
