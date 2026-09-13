"""Did anything actually happen during this run?

The scheduler used to replay a failed job by re-running the whole agent
request, up to three times. A job that sent the mail and then tripped on the
next step sent it again on the retry — the agent has no memory of what it
already did, and the request is natural language, not a resumable plan.

Deciding that needs one fact: was a tool called before it broke. This module
carries it, recorded where every agent run already passes
(`runner_utils.run_agent_simple`).

**Any tool counts.** Not just the writes. At the orchestrator level a "tool"
is usually a whole worker agent, so a call that looks like a read is a nested
run that may have written anything. This is the same conservative line
`web_interface.chat_stream` already draws for its own retry:

    # Once ANY tool/worker has been invoked, the run has side effects
    # ... so we must NOT retry the whole workflow

The cost is a job that fails after a harmless read is not retried either. That
is the cheaper mistake: a briefing that skips one morning is an annoyance, a
duplicated invoice is not.

The list is mutable on purpose. A child task copies the context but shares the
list object, so tool calls made inside nested runs are visible to the caller
that started the run.
"""

from contextvars import ContextVar
from typing import List, Optional, Tuple

_calls_var: ContextVar[Optional[List[str]]] = ContextVar("run_tool_calls", default=None)
_confirmations_var: ContextVar[Optional[List[str]]] = ContextVar(
    "run_confirmations", default=None
)
# (tool name, status) for every tool response that reported one. A tool that
# says "unknown" in its result used to reach the step contract as ordinary
# prose and count as success.
_outcomes_var: ContextVar[Optional[List[tuple]]] = ContextVar(
    "run_tool_outcomes", default=None
)


def start_run() -> None:
    """Begin tracking a run. Call once before dispatching the agent."""
    _calls_var.set([])
    _confirmations_var.set([])
    _outcomes_var.set([])


def ensure_run() -> None:
    """Begin tracking only if nothing is tracking yet.

    plan-execute may run inside a scheduled job that already started a run.
    Calling start_run() there would wipe that job's ledger, and the ledger is
    what decides whether replaying the job is safe.
    """
    if _calls_var.get() is None:
        start_run()


def note_tool_call(name: str) -> None:
    """Record that a tool was invoked. Inert outside a tracked run."""
    calls = _calls_var.get()
    if calls is not None:
        calls.append(name)


def note_needs_confirmation(action_id: str) -> None:
    """Record that the approval gate stopped to ask. Inert outside a run.

    Takes the action id — "tool:digest" — not the bare tool name. Two calls of
    the same tool with different arguments are different actions, and a list
    of names cannot tell them apart, so a second call could look like the
    first one still waiting.

    Read as a status rather than inferred from the answer text: a scheduled
    job that stopped because it needed a person did not fail, and calling it
    FAILED sends someone looking for a bug that is not there.
    """
    pending = _confirmations_var.get()
    if pending is not None:
        pending.append(action_id)


def confirmations_needed() -> Tuple[str, ...]:
    """Action ids this run could not do without being asked first."""
    return tuple(_confirmations_var.get() or ())


def action_label(action_id: str) -> str:
    """The tool name inside an action id, for saying it out loud."""
    return str(action_id).split(":", 1)[0]


def note_tool_outcome(tool_name: str, status: str) -> None:
    """Record a status a tool reported in its own result.

    The tools that cannot know whether they succeeded now say so — "unknown"
    from an HA command whose answer was lost, from a Google write whose
    connection dropped. That has to survive the trip to whoever is deciding
    if a step is done; the final text will not carry it.
    """
    outcomes = _outcomes_var.get()
    if outcomes is not None and status:
        outcomes.append((tool_name, str(status)))


def tool_outcomes() -> Tuple[tuple, ...]:
    """(tool, status) pairs reported so far, in order."""
    return tuple(_outcomes_var.get() or ())


def tool_calls() -> Tuple[str, ...]:
    """Tool names invoked so far, in order."""
    return tuple(_calls_var.get() or ())


def anything_happened() -> bool:
    """True when this run may have changed something outside the process."""
    return bool(_calls_var.get())


def clear() -> None:
    """Stop tracking. Mostly for tests."""
    _calls_var.set(None)
    _confirmations_var.set(None)
    _outcomes_var.set(None)
