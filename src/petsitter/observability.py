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


_trace: contextvars.ContextVar[Any] = contextvars.ContextVar("petsitter_trace", default=None)


def start_trace() -> contextvars.Token:
    """Begin collecting a structured trace of hook activity for this request.

    Only the playground turns this on.  When no trace is active
    ``trace_event`` is a no-op, so the normal request path is unaffected.
    """
    return _trace.set([])


def reset_trace(token: contextvars.Token) -> None:
    _trace.reset(token)


def get_trace() -> list[dict] | None:
    return _trace.get()


def trace_event(stage: str, trick: Any = None, **detail) -> None:
    """Record one pipeline step: which stage, which trick, what changed."""
    events = _trace.get()
    if events is None:
        return
    entry: dict[str, Any] = {"stage": stage}
    if trick is not None:
        # The class name is what the dashboard puts in data-name, so it is
        # enough to light up the right row without threading ids through.
        entry["trick"] = trick if isinstance(trick, str) else type(trick).__name__
    entry.update(detail)
    events.append(entry)


_request_meta: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "petsitter_request_meta", default=None
)


def start_request_meta(**initial) -> contextvars.Token:
    """Open the metadata channel that travels alongside a request's payload.

    Hooks see the conversation, but not everything about the request that
    produced it: ``post_hook`` is handed a message list with no way back to the
    tools, headers, or model that came with it.  Rather than have each trick
    stash that on ``self`` - which is shared across concurrent requests and so
    races - the proxy opens one dict per request here.  Being a contextvar it
    is per-task, so two requests in flight cannot see each other's.

    Tricks may also use it as scratch space to carry their own state between
    hooks within a single request.
    """
    return _request_meta.set(dict(initial))


def reset_request_meta(token: contextvars.Token) -> None:
    _request_meta.reset(token)


def request_meta() -> dict:
    """The current request's metadata, or an inert dict outside a request."""
    meta = _request_meta.get()
    return meta if meta is not None else {}


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
