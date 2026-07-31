"""Request-scoped observability for petsitter.

Provides per-request correlation ids and routing of request pipeline logs to
the trickset that handled the request.

Logs emitted during a chat request are written to the active trickset's own
log file (see ``Trickset.get_logger``) and are prefixed with a short request
id so they can be correlated across files:

    [ab12cd34] trickset 'gemma4' matched (X-Title='*' Model='gemma4*')

Lifecycle logs (install/uninstall/startup/shutdown) go to the trickset's log
file even when no request is running.
"""

import contextvars
import logging
import uuid
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".cache" / "petsitter"
TRICKSET_LOG_DIR = LOG_DIR / "tricksets"

_base = logging.getLogger("petsitter")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("petsitter_request_id", default="")
_current_trickset: contextvars.ContextVar[Any] = contextvars.ContextVar("petsitter_current_trickset", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def set_request_id(rid: str) -> contextvars.Token:
    return _request_id.set(rid)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def set_current_trickset(trickset: Any) -> contextvars.Token:
    return _current_trickset.set(trickset)


def reset_current_trickset(token: contextvars.Token) -> None:
    _current_trickset.reset(token)


def request_tag() -> str:
    rid = _request_id.get()
    return f"[{rid}] " if rid else ""


def get_logger() -> logging.Logger:
    """Return the logger for the currently active trickset.

    Falls back to the base ``petsitter`` logger when no request is running or
    no trickset has been selected yet.
    """
    ts = _current_trickset.get()
    if ts is not None:
        return ts.get_logger()
    return _base
