"""Run a blocking Home Assistant tool off the event loop, with a real deadline.

ADK calls a synchronous tool inline (google/adk/tools/function_tool.py:228-235
checks `iscoroutinefunction` and otherwise just calls it), so every `urlopen`
and every `time.sleep` inside the HA tools froze the whole process — the web
server, SSE and the voice websocket with it. The worst case was never a sleep:
`_volume_step_to` walks up to 30 steps, each spending two 10s HTTP timeouts,
so one "postavi glasnoću na 20" could hold the loop for ten minutes.

Two halves that only work as a pair:

* **The thread carries an absolute deadline.** `asyncio.to_thread` cannot be
  cancelled, so a timeout on the awaiting side does not stop the work — only
  the thread checking its own clock does. Every HTTP timeout is clamped to the
  time that is left, and every retry loop asks before another round. That is
  what makes the thread always return.
* **The device lock lives in the thread, not in the loop.** An `asyncio.Lock`
  released when the caller gives up would let the next command reach the TV
  while the abandoned one is still sending keys. A `threading.Lock` taken and
  released inside the worker is held for exactly as long as the work runs,
  whatever the caller does — and it is safe to hold precisely because the
  deadline above guarantees the work ends.

The lock key is the **device**, not the entity: `remote.tv` and
`media_player.tv` are one television, and giving them separate queues would
let "otvori aplikaciju" and "promijeni kanal" interleave on it again.

Timeouts are answered honestly. A command that never left the process is
`aborted`; one that may already have reached the device is `unknown`. Telling
the agent "failed" for the second would invite it to try again.
"""

import asyncio
import functools
import logging
import os
import socket
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Absolute monotonic time this operation must be finished by. Set inside the
# worker thread; asyncio.to_thread copies the caller's context, so the value
# is visible to the sync helpers without threading it through every signature.
_deadline_var: ContextVar[Optional[float]] = ContextVar("ha_deadline", default=None)

# Did anything actually go out to Home Assistant during this operation? Decides
# between "aborted" and "unknown" when the deadline hits.
_dispatched_var: ContextVar[int] = ContextVar("ha_dispatched", default=0)

# One lock per physical device, shared by all of its entities.
_DEVICE_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Never hand urlopen a timeout so small it is guaranteed to fail.
_MIN_HTTP_TIMEOUT = 0.5


class HAOperationError(Exception):
    """The operation did not complete. Which subclass says what that means."""


class OperationAborted(HAOperationError):
    """Stopped before anything went out. Nothing happened."""


class OutcomeUnknown(HAOperationError):
    """Something went out and the answer was lost. It may have been done.

    Raised both when the budget runs out mid-operation and when a real socket
    timeout swallows the reply to a command — those are the same situation as
    far as the caller is concerned, and neither is a plain failure.
    """


def _stop(message: str) -> "HAOperationError":
    """The right exception for where this operation got to."""
    if _dispatched():
        return OutcomeUnknown(message)
    return OperationAborted(message)


# --- deadline, as seen from inside the worker thread ------------------------

def remaining_seconds() -> Optional[float]:
    """Seconds left, or None when this call is not running under a deadline."""
    deadline = _deadline_var.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def note_dispatch() -> None:
    """Record that a request is about to leave for Home Assistant."""
    _dispatched_var.set(_dispatched_var.get() + 1)


def _dispatched() -> bool:
    return _dispatched_var.get() > 0


def check_deadline(what: str = "operacija") -> None:
    """Raise if there is no time left. Call before starting another round."""
    left = remaining_seconds()
    if left is not None and left <= 0:
        raise _stop(f"{what}: isteklo vrijeme")


def clamp_timeout(default: float) -> float:
    """The network timeout to actually use: never more than the time left.

    There is no floor. Handing back a 0.5s minimum when 0.05s remained was a
    hole in exactly the direction that matters — the call would then outlive
    the budget it was supposed to respect. Too little time left to try is a
    reason to stop before sending, not to send anyway.
    """
    left = remaining_seconds()
    if left is None:
        return default
    if left < _MIN_HTTP_TIMEOUT:
        raise _stop("nema dovoljno vremena za još jedan poziv")
    return min(default, left)


def proves_nothing_was_sent(exc: BaseException) -> bool:
    """Can we prove the request never left this machine?

    Only a refused connection or a host that would not resolve prove it.
    Everything else — a read timeout, a reset mid-flight, a broken pipe —
    happens on the wire, where the command may already have arrived and been
    acted on.

    So the question is asked in this direction on purpose. Listing the
    failures that mean "unknown" leaves every unlisted one silently counted
    as proof of failure, which is how a plain ConnectionResetError came back
    as "HA API nedostupan" for a command the television had already carried
    out. The default has to be the conservative one.
    """
    for candidate in (exc, getattr(exc, "reason", None)):
        if isinstance(candidate, (ConnectionRefusedError, socket.gaierror)):
            return True
    return False


def sleep(seconds: float) -> float:
    """Sleep, but never past the deadline. Returns how long it actually slept.

    Raises when the deadline is already gone, so a warm-up wait cannot quietly
    swallow the whole budget.
    """
    check_deadline("čekanje")
    left = remaining_seconds()
    actual = seconds if left is None else min(seconds, max(0.0, left))
    if actual > 0:
        time.sleep(actual)
    return actual


# --- the decorator ----------------------------------------------------------

def _device_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _DEVICE_LOCKS.setdefault(key, threading.Lock())


def _resolve_deadline(env_var: str, default: float) -> float:
    try:
        return float(os.getenv(env_var, str(default)))
    except (TypeError, ValueError):
        return default


def _failure_result(exc: HAOperationError, tool_name: str) -> Dict[str, Any]:
    if isinstance(exc, OutcomeUnknown):
        return {
            "error": (
                f"{tool_name}: naredba je poslana, ali potvrda nije stigla — "
                f"ne znam je li izvršena. Provjeri stanje prije ponavljanja. "
                f"({exc})"
            ),
            "outcome": "unknown",
        }
    return {
        "error": f"{tool_name}: prekinuto prije slanja, ništa nije izvršeno. ({exc})",
        "outcome": "aborted",
    }


def _run_in_thread(
    fn: Callable[..., Any],
    args: tuple,
    kwargs: dict,
    *,
    budget: float,
    device: Optional[str],
) -> Any:
    """Body that runs on the worker thread: deadline, device lock, call."""
    _deadline_var.set(time.monotonic() + budget)
    _dispatched_var.set(0)

    lock = _device_lock(device) if device else None
    if lock is not None:
        # Queueing counts against the budget on purpose: a command that waited
        # out its whole window is stale, and "aborted, nothing sent" is a
        # better answer than acting on it minutes late.
        wait = remaining_seconds() or 0.0
        if not lock.acquire(timeout=max(0.0, wait)):
            raise OperationAborted(f"uređaj '{device}' je zauzet")

    try:
        result = fn(*args, **kwargs)
    finally:
        if lock is not None:
            lock.release()

    # Every network path in these tools clamps to the budget, so an overrun
    # means one of them slipped through. Say so loudly rather than quietly
    # returning a late success — but do not throw away work that did happen.
    overrun = remaining_seconds()
    if overrun is not None and overrun < 0:
        logger.error(
            "%s finished %.1fs past its deadline — some network path is not "
            "using the shared budget", fn.__name__, -overrun,
        )
    return result


def offload(
    *,
    device: Optional[str] = None,
    deadline_env: str = "HA_OP_DEADLINE_SECONDS",
    default_deadline: float = 45.0,
):
    """Make a blocking HA tool awaitable, bounded and serialized per device.

    `functools.wraps` keeps `__name__`, `__doc__` and (via `__wrapped__`) the
    signature, which is what ADK reads to build the tool schema — so the model
    sees exactly the same tool it saw before.
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            budget = _resolve_deadline(deadline_env, default_deadline)
            try:
                return await asyncio.to_thread(
                    _run_in_thread, fn, args, kwargs,
                    budget=budget, device=device,
                )
            except HAOperationError as exc:
                logger.warning("%s: %s", fn.__name__, exc)
                return _failure_result(exc, fn.__name__)

        return wrapper

    return decorator
