"""
Orchestration control callbacks.

ADK-native guardrails wired into the orchestrator via `after_tool_callback`.
The orchestrator's tools are the worker agents (wrapped as AgentTools), so this
callback fires on the worker -> orchestrator boundary. When a worker returns an
empty/failed result, we REPLACE the tool response with an explicit failure
instruction so the orchestrator LLM cannot silently proceed (e.g. emailing a
link to a document that was never actually written).

ADK contract (google/adk/flows/llm_flows/functions.py): if an after_tool_callback
returns a non-None value, it replaces the original function response. Returning
None keeps the original response unchanged.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ADK control-flow tools that hand control to a sub-agent. They return an empty
# tool result by design (the response comes from the target agent), so the
# empty-result guardrail must never fire on them.
_CONTROL_FLOW_TOOLS = {"transfer_to_agent"}


def _is_empty_result(tool_response: Any) -> bool:
    """True when a worker AgentTool result is empty/missing.

    Worker AgentTool responses are dicts shaped like ``{"result": "<text>"}``.
    An empty/whitespace ``result`` (or an empty response object) means the step
    did not actually produce anything — the exact scribe-empty-doc failure mode.
    """
    if tool_response is None:
        return True
    if isinstance(tool_response, dict):
        # Empty dict, or {"result": ""}/{"result": None}/whitespace.
        if not tool_response:
            return True
        if "result" in tool_response:
            result = tool_response.get("result")
            return result is None or (isinstance(result, str) and not result.strip())
        # Other dict shapes (e.g. raw error dicts) are left to the LLM; we only
        # hard-fail on the proven empty-result case to avoid false positives.
        return False
    if isinstance(tool_response, str):
        return not tool_response.strip()
    return False


# How many times one worker has come back empty inside the current run.
# Same reasoning as the approval gate's hold counter: an instruction in a tool
# result is advice, and a model that ignores it once will ignore it twice.
_empty_results_this_run: Dict[tuple, int] = {}


def _count_empty(tool_name: str) -> int:
    from services import approvals

    key = (approvals.current_session(), tool_name)
    count = _empty_results_this_run.get(key, 0) + 1
    _empty_results_this_run[key] = count
    if len(_empty_results_this_run) > 2048:  # bounded memory
        _empty_results_this_run.clear()
    return count


def reset_empty_results(session_id: Optional[str] = None) -> None:
    """A new user message starts the count over."""
    from services import approvals

    session = session_id or approvals.current_session()
    for key in [k for k in _empty_results_this_run if k[0] == session]:
        del _empty_results_this_run[key]


def validate_worker_result(
    *,
    tool: Any,
    args: Any,
    tool_context: Any,
    tool_response: Any,
) -> Optional[dict]:
    """after_tool_callback: block silent empty/failed worker results.

    Returns an overriding response dict on failure (forcing the orchestrator to
    stop and report), or None to keep the original response.
    """
    tool_name = getattr(tool, "name", "unknown")

    # ADK control-flow tools hand control to a sub-agent and legitimately return
    # an empty tool result — the real response is produced by the target agent.
    # Treating that as a failure wrongly hijacks the turn with an error message.
    if tool_name in _CONTROL_FLOW_TOOLS:
        return None

    if _is_empty_result(tool_response):
        attempts = _count_empty(tool_name)
        logger.error(
            "[CONTROL] Worker '%s' returned an empty result (args=%s, attempt %d) — "
            "overriding with failure instruction to halt workflow.",
            tool_name,
            str(args)[:200],
            attempts,
        )

        if attempts >= 3:
            # Telling the model to stop is advice, and advice is what the
            # approval gate already watched a model ignore four times in one
            # turn. An "error" key is the shape the loop guard counts and the
            # model treats as terminal, so this ends the turn instead of
            # spending more round-trips on a worker that keeps coming back
            # empty.
            return {
                "error": (
                    f"STOP: agent '{tool_name}' je {attempts} puta vratio prazan "
                    f"rezultat. Ne zovi ga više i ne zovi alate koji ovise o "
                    f"njemu. Javi korisniku da taj korak nije uspio i završi "
                    f"odgovor."
                )
            }

        return {
            "result": (
                f"⚠️ TOOL FAILURE: agent '{tool_name}' je vratio prazan rezultat — "
                f"korak NIJE dovršen. Prema Rule 3, ODMAH zaustavi workflow i javi "
                f"korisniku da je '{tool_name}' zakazao. NE pozivaj alate koji ovise o "
                f"ovom rezultatu (npr. slanje maila s linkom na dokument koji možda nije "
                f"kreiran)."
            )
        }

    return None
