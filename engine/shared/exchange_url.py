"""
shared/exchange_url.py — which venue is this process actually dialling, and why?

Every client (trader, broker, the house bots, the launcher) used to rebuild
the same expression by hand::

    os.environ.get("EXCHANGE_URL") or f"ws://{EXCHANGE_HOST or localhost}:{PORT}"

…and none of them said out loud what they had resolved. That silence is the
single most expensive rough edge in the course: `make register` writes
``EXCHANGE_HOST=<hosted arena IP>`` into `.env`, `shared.envfile.load_env()`
copies it into os.environ at import time, and a student who then starts a
local exchange watches their bot loop on

    Could not reach exchange: timed out during opening handshake

with nothing anywhere to reveal that it is dialling AWS, not localhost.

So this module resolves the URL in ONE place and returns its PROVENANCE with
it — the shell, the `.env` file `make register` wrote, or the built-in
default — and formats the one line every client logs at connect time:

    Connecting to ws://54.1.2.3:8765 (EXCHANGE_HOST from .env
        — set EXCHANGE_HOST=localhost for local play)

Stdlib only, and nothing here raises: a diagnostic must never be the reason
a bot fails to start.
"""

from __future__ import annotations

import os

from shared import envfile

DEFAULT_HOST = "localhost"
DEFAULT_PORT = "8765"

#: What to tell a student whose URL came out of a stale `.env`.
LOCAL_HINT = "set EXCHANGE_HOST=localhost for local play"

#: Source label when neither EXCHANGE_URL nor EXCHANGE_HOST is set anywhere.
SOURCE_DEFAULT = "built-in default"
#: Source label for a URL handed to a client directly (multi-venue fan-out).
SOURCE_EXPLICIT = "caller-supplied URL"

_SHELL = "the shell"
_ENV_FILE = ".env"

# Venues already announced by this process, so a reconnect loop cannot spam
# the line every three seconds while a multi-venue bot still gets one line
# per distinct venue.
_announced: set[str] = set()


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────

def _origin(key: str) -> str:
    """Where os.environ[key] came from: the shell, or the .env file."""
    return _ENV_FILE if envfile.came_from_env_file(key) else _SHELL


def _label(key: str) -> str:
    return f"{key} from {_origin(key)}"


def resolve_exchange_url(env_path: str | os.PathLike = ".env") -> tuple[str, str]:
    """Return ``(url, source)`` for the single venue this process should dial.

    Precedence (unchanged from the expression this replaces):

    1. ``EXCHANGE_URL`` — a full URL, so ``wss://`` hosted play works
    2. ``EXCHANGE_HOST`` + ``EXCHANGE_PORT``
    3. ``ws://localhost:8765``

    `source` names the ORIGIN of whichever variable won — e.g.
    ``"EXCHANGE_HOST from .env"``, ``"EXCHANGE_URL from the shell"``, or
    ``"built-in default"``. `.env` is loaded first (shell values still win,
    exactly as before) so the answer is the same one the client will get.
    """
    envfile.load_env(env_path)

    url = (os.environ.get("EXCHANGE_URL") or "").strip()
    if url:
        return url, _label("EXCHANGE_URL")

    port = (os.environ.get("EXCHANGE_PORT") or "").strip() or DEFAULT_PORT
    host = (os.environ.get("EXCHANGE_HOST") or "").strip()
    if host:
        return f"ws://{host}:{port}", _label("EXCHANGE_HOST")
    return f"ws://{DEFAULT_HOST}:{port}", SOURCE_DEFAULT


def exchange_urls(env_path: str | os.PathLike = ".env") -> tuple[list[str], str]:
    """Return ``(urls, source)`` for the multi-venue list (Level 6).

    ``EXCHANGE_URLS`` is a comma-separated list of venues to quote or
    arbitrage on simultaneously; with it unset the single resolved URL is the
    whole list.
    """
    envfile.load_env(env_path)
    raw = (os.environ.get("EXCHANGE_URLS") or "").strip()
    if raw:
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if urls:
            return urls, _label("EXCHANGE_URLS")
    url, source = resolve_exchange_url(env_path)
    return [url], source


def source_of(url: str, env_path: str | os.PathLike = ".env") -> str:
    """Best explanation of where `url` came from.

    A multi-venue client is handed one URL out of ``EXCHANGE_URLS``; a
    single-venue one uses the resolved default. Anything else was built by
    the caller (a test, or an explicit constructor argument).
    """
    url = (url or "").strip()
    if not url:
        return SOURCE_DEFAULT
    urls, source = exchange_urls(env_path)
    if url in urls:
        return source
    return SOURCE_EXPLICIT


# ─────────────────────────────────────────────────────────────────────────────
# The one line every client logs
# ─────────────────────────────────────────────────────────────────────────────

def format_connect_line(url: str, source: str) -> str:
    """The connect-time diagnostic, with the local-play hint when earned."""
    hint = f" — {LOCAL_HINT}" if source.endswith(f"from {_ENV_FILE}") else ""
    return f"Connecting to {url} ({source}{hint})"


def describe_exchange_url(url: str | None = None,
                          env_path: str | os.PathLike = ".env") -> str:
    """Format the connect line for `url` (or for the resolved default)."""
    if url:
        return format_connect_line(url, source_of(url, env_path))
    resolved, source = resolve_exchange_url(env_path)
    return format_connect_line(resolved, source)


def connect_line_once(url: str | None = None,
                      env_path: str | os.PathLike = ".env") -> str | None:
    """The connect line the FIRST time this venue is dialled, else None.

    Callers log it with whatever they already use — ``logger.info(...)`` or a
    rich console — so a reconnect loop stays quiet while a multi-venue bot
    still reports every distinct venue exactly once::

        line = connect_line_once(self.exchange_url)
        if line:
            logger.info("%s", line)
    """
    if url:
        resolved, source = url, source_of(url, env_path)
    else:
        resolved, source = resolve_exchange_url(env_path)
    if resolved in _announced:
        return None
    _announced.add(resolved)
    return format_connect_line(resolved, source)


#: The env var that names the address a VENUE binds. Deliberately NOT
#: EXCHANGE_HOST — that one is the address clients dial.
BIND_KEY = "EXCHANGE_BIND"

#: Addresses a process can actually bind on any machine.
LOCAL_BIND_HOSTS = ("0.0.0.0", "::", "localhost", "127.0.0.1", "::1", "")


#: What to tell a student whose venue cannot bind the address it was given.
BIND_HINT = f"set {BIND_KEY}=0.0.0.0 (or unset it) to bind every interface"


def bind_line_once(host: str, port: int | str) -> str | None:
    """The line a VENUE logs once: which address it binds, and its source.

    The mirror image of the client problem, and the nastier half of it:
    ``exchange/config.HOST`` used to be the BIND address as well as the
    client-facing one, so a stale `.env` holding the hosted arena's IP made a
    student's own exchange try to bind an address that does not exist on their
    laptop — ``OSError: [Errno 49] Can't assign requested address``. The bind
    address is now its own variable, ``EXCHANGE_BIND`` (default 0.0.0.0), and
    this line names whichever variable actually produced `host`.
    """
    key = f"bind:{host}:{port}"
    if key in _announced:
        return None
    _announced.add(key)
    host_env = (os.environ.get("EXCHANGE_HOST") or "").strip()
    if os.environ.get(BIND_KEY):
        source = _label(BIND_KEY)
    elif host_env and str(host) == host_env:
        # A caller still passing EXCHANGE_HOST as the bind address (or a
        # venue explicitly told to bind it) — name the variable it came from.
        source = _label("EXCHANGE_HOST")
    else:
        source = SOURCE_DEFAULT
    line = f"Serving on {host}:{port} ({source})"
    if str(host) not in LOCAL_BIND_HOSTS:
        line += (f" — {host} is not an address this machine can bind; "
                 f"EXCHANGE_HOST only tells CLIENTS where to dial, so "
                 f"{BIND_HINT} ({LOCAL_HINT})")
    return line


def reset_connect_log() -> None:
    """Forget which venues have been announced (tests; a fresh session)."""
    _announced.clear()
