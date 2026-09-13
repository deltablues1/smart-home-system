"""
ADK Agent Factory

Factory functions for creating ADK-compliant agents using LlmAgent primitives.
Replaces custom BaseAgent with native Google ADK agents.
"""

import os
from typing import List, Optional, Dict, Any, Union
from pathlib import Path
import logging
from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.genai import types

logger = logging.getLogger(__name__)


# ---- Provider routing (Gemini default; Claude via LiteLLM when enabled) ----

def _gemini_pinned_agents() -> set:
    """Agents that must stay on Gemini/Vertex (e.g. Vertex RAG tools)."""
    base = {"socrates", "christian_guide"}
    for a in os.getenv("CLAUDE_GEMINI_ONLY_AGENTS", "").split(","):
        a = a.strip().lower()
        if a:
            base.add(a)
    return base


# Content/reasoning workers default to the stronger ("pro") Claude tier; glue
# and routing agents (planner, summarizer, orchestrator, secretary, tracker,
# rolodex, librarian, scraper, voice_qa) fall through to the cheap tier map.
# Fully overridable per agent via CLAUDE_AGENT_MODELS.
_DEFAULT_STRONG_AGENTS = {
    "analyst", "mailer", "researcher", "scribe", "synthesizer",
}


def _claude_agent_overrides() -> Dict[str, str]:
    """Parse CLAUDE_AGENT_MODELS='mailer=claude-sonnet-4-6,researcher=claude-opus-4-8'."""
    out: Dict[str, str] = {}
    for pair in os.getenv("CLAUDE_AGENT_MODELS", "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k and v:
                out[k] = v
    return out


def _claude_model_for(model: str, agent_name: Optional[str] = None) -> str:
    """Pick the Claude model for an agent: explicit env override > built-in
    strong-agent default > Gemini-tier mapping."""
    name = (agent_name or "").lower()

    override = _claude_agent_overrides().get(name)
    if override:
        return override

    if name in _DEFAULT_STRONG_AGENTS:
        return os.getenv("CLAUDE_PRO_MODEL", "claude-sonnet-5")

    m = (model or "").lower()
    if "pro" in m:
        return os.getenv("CLAUDE_PRO_MODEL", "claude-sonnet-5")
    if "lite" in m:
        return os.getenv("CLAUDE_LITE_MODEL", "claude-sonnet-5")
    return os.getenv("CLAUDE_FLASH_MODEL", "claude-sonnet-5")


# Claude Sonnet 5 / Opus 4.7+ / Fable reject non-default sampling params
# (temperature/top_p/top_k -> HTTP 400) and run adaptive thinking when the
# thinking param is omitted. For those models we drop temperature and give
# max_output_tokens headroom: thinking tokens count against the cap, and a
# truncated response can cut a tool call's JSON arguments mid-stream (observed
# as scribe calling format_markdown_for_docs with the mandatory `markdown`
# argument missing). 8192 leaves room for thinking + a large tool payload.
_CLAUDE_NO_SAMPLING_PREFIXES = (
    "claude-sonnet-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable",
    "claude-mythos",
)
_CLAUDE_THINKING_MIN_OUTPUT_TOKENS = 8192


def _claude_rejects_sampling(claude_model: str) -> bool:
    return (claude_model or "").lower().startswith(_CLAUDE_NO_SAMPLING_PREFIXES)


def _use_anthropic(agent_name: Optional[str]) -> bool:
    if os.getenv("LLM_PROVIDER", "gemini").lower() != "anthropic":
        return False
    return (agent_name or "").lower() not in _gemini_pinned_agents()


def claude_safe_generation_kwargs(
    agent_name: Optional[str],
    model: Optional[str] = None,
    *,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Generation kwargs an agent can actually send to the model behind it.

    Agents that assign generate_content_config by hand after create_adk_agent
    overwrite what the factory decided, and so lose both rules below. The
    planner, the summarizer and the orchestrator all did, which is why every
    request through them died on `temperature is deprecated for this model`
    the moment their tier resolved to Sonnet 5.
    """
    no_sampling = _use_anthropic(agent_name) and _claude_rejects_sampling(
        _claude_model_for(model, agent_name)
    )
    kwargs: Dict[str, Any] = {}
    if temperature is not None and not no_sampling:
        kwargs["temperature"] = temperature
    if max_output_tokens is not None:
        # Thinking tokens come out of the same budget, so a cap sized for a
        # plain answer can truncate one mid-JSON.
        if no_sampling:
            max_output_tokens = max(
                max_output_tokens, _CLAUDE_THINKING_MIN_OUTPUT_TOKENS
            )
        kwargs["max_output_tokens"] = max_output_tokens
    return kwargs


def _claude_retries() -> int:
    """LiteLLM num_retries — retries Anthropic 429/5xx with exponential backoff
    (honors the retry-after header). Helps burst rate-limits self-heal."""
    try:
        return int(os.getenv("CLAUDE_RETRY_ATTEMPTS", "4"))
    except ValueError:
        return 4


def _effective_model_name(model, agent_name: Optional[str] = None) -> str:
    """The model string we actually run with — for logging and token labels."""
    if isinstance(model, str) and _use_anthropic(agent_name):
        return "anthropic/" + _claude_model_for(model, agent_name)
    return model if isinstance(model, str) else getattr(model, "model", str(model))


def _build_model(model: str, agent_name: Optional[str] = None):
    """Build the model object for an agent.

    Provider is chosen by ``LLM_PROVIDER`` (default ``gemini``). When set to
    ``anthropic``, agents not pinned to Gemini are routed to Claude through
    ADK's LiteLLM wrapper; everything else keeps the Gemini path below.

    Wrap a model name in a Gemini model that retries transient LLM failures.

    The Vertex AI Gemini endpoint returns 429 RESOURCE_EXHAUSTED when the
    per-minute quota bucket is momentarily empty (bursts of requests). Those
    calls are made by the ADK runner itself, so our Workspace-tool retry layer
    (tools/resilience/retry_handler) never sees them. Attaching HttpRetryOptions
    retries ONLY the failed generate_content HTTP call with backoff — the agent
    loop is not re-run, so tool side effects are never duplicated.

    Configurable via env (defaults cover a ~60s per-minute quota window):
      LLM_RETRY_ATTEMPTS (default 5; <=1 disables and uses the plain string)
      LLM_RETRY_INITIAL_DELAY (s, default 2)
      LLM_RETRY_MAX_DELAY (s, default 60)
      LLM_RETRY_EXP_BASE (default 2)
    """
    # Provider routing: Claude via LiteLLM when enabled and not pinned to Gemini.
    if _use_anthropic(agent_name):
        try:
            from google.adk.models.lite_llm import LiteLlm

            claude = _claude_model_for(model, agent_name)
            logger.info(
                "Routing agent '%s' to Claude via LiteLLM: anthropic/%s",
                agent_name, claude,
            )
            return LiteLlm(model=f"anthropic/{claude}", num_retries=_claude_retries())
        except Exception as e:
            logger.warning(
                "LiteLLM/Claude unavailable for '%s' (%s); falling back to Gemini.",
                agent_name, e,
            )

    try:
        attempts = int(os.getenv("LLM_RETRY_ATTEMPTS", "5"))
    except ValueError:
        attempts = 5

    if attempts <= 1:
        return model

    try:
        retry_options = types.HttpRetryOptions(
            attempts=attempts,
            initial_delay=float(os.getenv("LLM_RETRY_INITIAL_DELAY", "2")),
            max_delay=float(os.getenv("LLM_RETRY_MAX_DELAY", "60")),
            exp_base=float(os.getenv("LLM_RETRY_EXP_BASE", "2")),
            http_status_codes=[429, 503],
        )
        return Gemini(model=model, retry_options=retry_options)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            f"Could not build Gemini model with retry_options ({e}); "
            "using plain model string (no LLM retry)"
        )
        return model


def _make_usage_callback(agent_name: str, model_str: str):
    """Build an ADK after_model_callback that records per-agent token usage.

    Framework-level, provider-agnostic — fires for Gemini today and for any
    LiteLLM-routed model (Claude/GPT) later, so the numbers compare directly.
    Fully defensive: any extraction failure is swallowed so it can never break
    a model turn.
    """

    def _after_model(callback_context, llm_response):
        try:
            from tools.observability.token_stats import get_token_stats

            um = getattr(llm_response, "usage_metadata", None)
            if um is None:
                return None
            prompt = getattr(um, "prompt_token_count", 0) or 0
            output = getattr(um, "candidates_token_count", 0) or 0
            cached = getattr(um, "cached_content_token_count", 0) or 0
            total = getattr(um, "total_token_count", 0) or 0
            name = getattr(callback_context, "agent_name", None) or agent_name
            get_token_stats().record(name, model_str, prompt, output, cached, total)

            from tools.observability.token_stats import cost_of
            from services import budget

            spend = cost_of(model_str, prompt, output, cached)
            if spend:
                budget.record(spend)
        except Exception:  # never let accounting break inference
            pass
        return None  # do not modify the response

    return _after_model


class DailyBudgetExceeded(RuntimeError):
    """Today's model spend is used up."""


def _budget_before_model(callback_context, llm_request):
    """Refuse to spend past the day's ceiling.

    False wake-word triggers drained the credit overnight on 2026-08-30 while
    token accounting recorded every call and did nothing with the total. A
    console limit is the backstop; this is the part that can say why it stopped.
    """
    try:
        from services import budget

        if not budget.over_budget():
            return None
        spent, ceiling = budget.spent_today(), budget.daily_budget_usd()
    except Exception:  # a broken ledger must not stop the house
        return None

    logger.error("Daily LLM budget exhausted: $%.2f of $%.2f", spent, ceiling)
    raise DailyBudgetExceeded(
        f"Dnevni budžet je potrošen: ${spent:.2f} od ${ceiling:.2f}. "
        "Zaustavljam pozive modela do ponoći. Ako je ovo pogreška, podigni "
        "DAILY_LLM_BUDGET_USD ili provjeri što troši."
    )


# ---- Tool retry loop guard ------------------------------------------------
# Without this, a model may repeat the exact same failing tool call forever
# (observed live: scribe called format_markdown_for_docs with missing args
# every ~55s for 15+ minutes, each retry a billed LLM call). After
# _TOOL_LOOP_WARN_AFTER identical failures the tool is short-circuited with a
# corrective error message; after _TOOL_LOOP_ABORT_AFTER the run is killed.

_TOOL_LOOP_WARN_AFTER = 3
_TOOL_LOOP_ABORT_AFTER = 6
_tool_failure_counts: Dict[tuple, int] = {}


def _tool_loop_key(tool, args, tool_context) -> tuple:
    import json as _json

    invocation = getattr(tool_context, "invocation_id", None) or "global"
    tool_name = getattr(tool, "name", None) or str(tool)
    try:
        args_key = _json.dumps(args, sort_keys=True, default=str)[:512]
    except Exception:
        args_key = str(args)[:512]
    return (invocation, tool_name, args_key)


def _short(value, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _log_tool_call(tool=None, args=None, tool_context=None, **_kwargs):
    """Log every function-tool call a sub-agent makes.

    Only the orchestrator's call into a sub-agent was logged, so whatever the
    sub-agent did next was invisible: three separate TV bugs had to be diagnosed
    against Home Assistant instead of the journal, because the log could not say
    whether tv_open_app had even run.
    """
    name = getattr(tool, "name", None) or getattr(tool, "__name__", "?")
    logger.info("[TOOL] %s(%s)", name, _short(args))
    return None


def _log_tool_result(tool=None, args=None, tool_context=None, tool_response=None, **_kwargs):
    name = getattr(tool, "name", None) or getattr(tool, "__name__", "?")
    logger.info("[TOOL=] %s -> %s", name, _short(tool_response))
    return None


def _tool_loop_before(tool=None, args=None, tool_context=None, **_kwargs):
    key = _tool_loop_key(tool, args, tool_context)
    count = _tool_failure_counts.get(key, 0)
    if count >= _TOOL_LOOP_ABORT_AFTER:
        _tool_failure_counts.pop(key, None)
        raise RuntimeError(
            f"Loop guard: tool '{key[1]}' failed {count} times with identical "
            "arguments — aborting the run."
        )
    if count >= _TOOL_LOOP_WARN_AFTER:
        logger.warning(
            "Loop guard: short-circuiting repeat of failing tool call %s (count=%d)",
            key[1], count,
        )
        return {
            "error": (
                f"LOOP GUARD: this exact call to '{key[1]}' already failed "
                f"{count} times. Do NOT repeat it. Provide ALL mandatory "
                "parameters with real values, try a different approach, or "
                "stop and report the problem to the user."
            )
        }
    return None


# ---- Per-tool call budget -------------------------------------------------
# The researcher's prompt says "at most 15 tool calls". On 2026-09-03 it made
# 16 google_search_simple calls in one run and was still going at 24 minutes,
# because a sentence in a prompt is not a limit. The loop guard above only
# catches the *same* failing call repeated; a model that keeps inventing new
# queries walks past it.
#
# The cap is per (run, tool), not per run: an orchestrator legitimately calls
# sixteen different workers, while sixteen calls to one search tool is a loop.

_tool_call_counts: Dict[tuple, int] = {}


def _tool_budget() -> int:
    try:
        return max(1, int(os.getenv("AGENT_MAX_CALLS_PER_TOOL", "12")))
    except ValueError:
        return 12


def _tool_budget_before(tool=None, args=None, tool_context=None, **_kwargs):
    invocation = getattr(tool_context, "invocation_id", None) or "global"
    name = getattr(tool, "name", None) or getattr(tool, "__name__", "?")
    key = (invocation, name)

    count = _tool_call_counts.get(key, 0) + 1
    _tool_call_counts[key] = count
    if len(_tool_call_counts) > 4096:  # bounded memory
        _tool_call_counts.clear()

    budget = _tool_budget()
    if count <= budget:
        return None

    logger.warning(
        "Tool budget exhausted: '%s' called %d times in one run (limit %d)",
        name, count, budget,
    )
    return {
        "error": (
            f"TOOL BUDGET: '{name}' has been called {count} times in this run, "
            f"over the limit of {budget}. Stop calling it. Write your answer "
            "from what you already have, and state plainly what you could not "
            "confirm."
        )
    }


def _is_agent_tool(tool) -> bool:
    """True for a sub-agent exposed as a tool (ADK AgentTool)."""
    try:
        from google.adk.tools.agent_tool import AgentTool

        if isinstance(tool, AgentTool):
            return True
    except Exception:
        pass
    # Duck-typed fallback: an AgentTool wraps an agent and nothing else does.
    return hasattr(tool, "agent") and hasattr(tool, "name")


def _agent_tool_args_guard(tool=None, args=None, tool_context=None, **_kwargs):
    """A sub-agent call with no request must not take the whole run down.

    google.adk.tools.agent_tool reads args['request'] directly, with no default,
    so a model emitting `scribe({})` raises KeyError out of the runner. On
    2026-09-03 that discarded thirteen minutes of finished research after the
    researcher had already returned its report. The loop guard only catches the
    second identical failure; this catches the first and hands the model
    something it can act on.
    """
    if not _is_agent_tool(tool):
        return None
    request = (args or {}).get("request")
    if isinstance(request, str) and request.strip():
        return None
    name = getattr(tool, "name", "?")
    logger.warning("Agent tool '%s' called without a request — returning a correction", name)
    return {
        "error": (
            f"MISSING ARGUMENT: '{name}' was called with no 'request'. Call it "
            "again with request=\"...\" containing the full text that agent "
            "needs — it has no memory of this conversation and cannot see any "
            "earlier result."
        )
    }


def _tool_loop_after(tool=None, args=None, tool_context=None, tool_response=None, **_kwargs):
    key = _tool_loop_key(tool, args, tool_context)
    failed = isinstance(tool_response, dict) and "error" in tool_response
    if failed:
        _tool_failure_counts[key] = _tool_failure_counts.get(key, 0) + 1
        if len(_tool_failure_counts) > 2048:  # bounded memory
            _tool_failure_counts.clear()
    else:
        _tool_failure_counts.pop(key, None)
    return None  # never alter the tool response


# Agents that consume external content (email bodies, web pages, documents)
# get a shared prompt-injection boundary appended to their instructions.
# NOTE: names must match the create_adk_agent(name=...) argument exactly —
# the main coordinator is "smart_orchestrator", not "orchestrator" (a stale
# "orchestrator" entry here once left the real orchestrator unprotected).
_UNTRUSTED_CONTENT_AGENTS = {
    "mailer", "librarian", "analyst", "scribe", "researcher",
    "smart_orchestrator", "scraper", "synthesizer",
    "secretary", "tracker", "rolodex", "briefing_summarizer",
    # Built outside this factory (christian_guide_adk.py) but reads a RAG
    # corpus, so it applies the rule itself by calling the helper below.
    "christian_guide",
}


# Agents holding at least one tool that services/approval_gate.py can stop,
# plus analyst and tracker, whose destructive tools have no gate at all and
# therefore need the second half of the same text. smart_home writes its own
# version against its own tools and is deliberately absent.
_CONFIRMATION_GATE_AGENTS = {
    "mailer", "librarian", "secretary", "tracker", "analyst",
    "smart_orchestrator",
}

# Agents owning at least one tool that can come back "unknown": the write may
# already have landed and only the answer was lost. Listed from the
# except-UnconfirmedWrite sites in tools/adk_tools/*.py plus
# gmail_send_message, which sets outcome="unknown" the same way.
_UNKNOWN_OUTCOME_AGENTS = {
    "mailer", "librarian", "analyst", "tracker", "rolodex", "scribe",
    "smart_orchestrator",
}


def load_shared_fragment(name: str) -> Optional[str]:
    """Load a shared instruction fragment from agents/shared/{name}.md."""
    fragment_path = Path(__file__).parent.parent / "shared" / f"{name}.md"
    if not fragment_path.exists():
        logger.debug(f"No shared fragment at {fragment_path}")
        return None
    try:
        return fragment_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to load shared fragment '{name}': {e}")
        return None


def _append_shared_fragment(instruction: str, fragment: Optional[str]) -> str:
    """Append a static instruction fragment on the cached side of the break."""
    if not fragment or fragment.strip() in instruction:
        return instruction

    from config.runtime_patches import CACHE_BREAK

    # The fragment is static, so it belongs on the cached side of the break.
    # Appending it blindly would push it past the marker and re-send it on
    # every request, right next to the volatile text it was meant to precede.
    static, sep, volatile = instruction.partition(CACHE_BREAK)
    if sep:
        instruction = static.rstrip()
        return (
            f"{instruction.rstrip()}\n\n---\n\n{fragment.strip()}\n"
            + CACHE_BREAK + volatile
        )
    return f"{instruction.rstrip()}\n\n---\n\n{fragment.strip()}\n"


def _append_untrusted_content_rule(name: str, instruction: str) -> str:
    """Append the untrusted-content boundary for content-consuming agents."""
    if name.lower() not in _UNTRUSTED_CONTENT_AGENTS:
        return instruction
    return _append_shared_fragment(
        instruction, load_shared_fragment("untrusted_content")
    )


def _append_confirmation_gate_rule(name: str, instruction: str) -> str:
    """Append the approval-gate protocol for agents that own gated tools.

    Skipped for smart_home: it already carries this protocol written against
    its own tools, and a second copy would only give the model two texts to
    reconcile.
    """
    if name.lower() not in _CONFIRMATION_GATE_AGENTS:
        return instruction
    return _append_shared_fragment(
        instruction, load_shared_fragment("confirmation_gate")
    )


def _append_unknown_outcome_rule(name: str, instruction: str) -> str:
    """Append the three-outcome protocol for agents with non-idempotent writes."""
    if name.lower() not in _UNKNOWN_OUTCOME_AGENTS:
        return instruction
    return _append_shared_fragment(
        instruction, load_shared_fragment("unknown_outcome")
    )


def load_instruction_file(agent_name: str) -> Optional[str]:
    """
    Load agent instructions from markdown file.

    Args:
        agent_name: Name of the agent (e.g., "mailer", "researcher")

    Returns:
        Instruction text or None if file not found

    Looks for instructions in: agents/{agent_name}/instructions.md
    """
    # Try to find instructions.md in agent's directory
    base_path = Path(__file__).parent.parent  # Go up to agents/
    instruction_path = base_path / agent_name / "instructions.md"

    if instruction_path.exists():
        try:
            with open(instruction_path, 'r', encoding='utf-8') as f:
                instructions = f.read()
            logger.debug(f"Loaded instructions for {agent_name} from {instruction_path}")
            return instructions
        except Exception as e:
            logger.warning(f"Failed to load instructions for {agent_name}: {e}")
            return None
    else:
        logger.debug(f"No instruction file found at {instruction_path}")
        return None


def create_adk_agent(
    name: str,
    model: str,
    instruction: Optional[str] = None,
    description: Optional[str] = None,
    tools: Optional[List] = None,
    sub_agents: Optional[List] = None,
    config: Optional[Dict[str, Any]] = None,
    load_instruction_from_file: bool = True,
    after_tool_callback: Optional[Any] = None,
    before_tool_callback: Optional[Any] = None
) -> LlmAgent:
    """
    Factory for creating ADK LlmAgent instances.

    This replaces the custom BaseAgent class with native ADK agents.

    Args:
        name: Unique agent name (e.g., "mailer", "researcher")
        model: Gemini model name (e.g., "gemini-3.5-flash", "gemini-2.5-pro")
        instruction: System instruction text. If None and load_instruction_from_file=True,
                    will try to load from agents/{name}/instructions.md
        description: Short description for AutoFlow routing (used when agent is a sub-agent)
        tools: List of ADK Tool objects (FunctionTool, google_search, etc.)
        sub_agents: List of child agents for hierarchical composition
        config: Additional configuration (temperature, max_tokens, etc.)
                Note: These are passed through Runner.run_async(), not directly to LlmAgent
        load_instruction_from_file: If True, attempts to load instruction from .md file

    Returns:
        LlmAgent instance

    Example:
        >>> from tools.adk_tools.gmail_adk_tools import get_gmail_adk_tools
        >>> mailer = create_adk_agent(
        ...     name="mailer",
        ...     model="gemini-3.5-flash",
        ...     description="Gmail specialist for email operations",
        ...     tools=get_gmail_adk_tools()
        ... )
    """
    # Load instruction from file if not provided
    if instruction is None and load_instruction_from_file:
        instruction = load_instruction_file(name)

    # Default instruction if still None
    if instruction is None:
        instruction = f"You are {name}, a specialized AI agent."
        logger.warning(f"No instruction found for {name}, using default")

    # Prompt-injection boundary for agents that read external content.
    instruction = _append_untrusted_content_rule(name, instruction)
    instruction = _append_confirmation_gate_rule(name, instruction)
    instruction = _append_unknown_outcome_rule(name, instruction)

    # Time-aware instructions must stay time-aware. Agents pass the raw
    # template; rendering it per invocation keeps a long-running process from
    # believing it is still whatever time it booted at.
    from agents.adk_agents.datetime_context import (
        has_datetime_placeholders,
        make_datetime_instruction,
    )
    if has_datetime_placeholders(instruction):
        instruction = make_datetime_instruction(
            instruction, os.getenv("USER_TIMEZONE", "Europe/Zagreb")
        )

    # Default description
    if description is None:
        description = f"{name.capitalize()} agent"

    # Log agent creation
    logger.info(
        f"Creating ADK agent: {name}",
        extra={
            "model": model,
            "has_tools": bool(tools),
            "tool_count": len(tools) if tools else 0,
            "has_sub_agents": bool(sub_agents),
            "sub_agent_count": len(sub_agents) if sub_agents else 0,
            "has_instruction_file": load_instruction_from_file and load_instruction_file(name) is not None
        }
    )

    # Create LlmAgent (wrap the model so transient LLM 429/503s are retried;
    # routes to Claude via LiteLLM when LLM_PROVIDER=anthropic and not pinned).
    agent_kwargs = dict(
        name=name,
        model=_build_model(model, name),
        instruction=instruction,
        description=description,
        tools=tools or [],
        sub_agents=sub_agents or []
    )
    # Tool-loop guard first, then any optional caller guardrail callbacks.
    # ADK accepts a list and stops at the first callback returning non-None
    # (before) / keeps the first non-None override (after) — the guard returns
    # None in the happy path, so caller callbacks still run.
    if tools:
        # The approval gate goes after the loop guard and before any caller
        # guardrail: a consequential call must be held for a real user turn no
        # matter which agent is making it, so it is wired in here rather than
        # left to each agent to remember.
        from services.approval_gate import approval_before_tool

        agent_kwargs["before_tool_callback"] = (
            [_log_tool_call, _agent_tool_args_guard, _tool_loop_before,
             _tool_budget_before, approval_before_tool, before_tool_callback]
            if before_tool_callback is not None
            else [_log_tool_call, _agent_tool_args_guard, _tool_loop_before,
                  _tool_budget_before, approval_before_tool]
        )
        agent_kwargs["after_tool_callback"] = (
            [_tool_loop_after, _log_tool_result, after_tool_callback]
            if after_tool_callback is not None else [_tool_loop_after, _log_tool_result]
        )
    else:
        if after_tool_callback is not None:
            agent_kwargs["after_tool_callback"] = after_tool_callback
        if before_tool_callback is not None:
            agent_kwargs["before_tool_callback"] = before_tool_callback

    # Per-agent token accounting (provider-agnostic; disable with TOKEN_STATS_ENABLED=false).
    if os.getenv("TOKEN_STATS_ENABLED", "true").lower() in ("1", "true", "yes", "on"):
        agent_kwargs["before_model_callback"] = _budget_before_model
        agent_kwargs["after_model_callback"] = _make_usage_callback(
            name, _effective_model_name(model, name)
        )

    agent = LlmAgent(**agent_kwargs)

    # Apply generate_content_config from config dict
    if config:
        # Sonnet 5 / Opus 4.7+ reject non-default sampling params outright and
        # spend part of max_output_tokens on adaptive thinking — strip the
        # former, give headroom on the latter.
        claude_no_sampling = _use_anthropic(name) and _claude_rejects_sampling(
            _claude_model_for(model, name)
        )

        gen_config_kwargs = {}
        if "temperature" in config:
            if claude_no_sampling:
                logger.info(
                    "Agent '%s': dropping temperature=%s (model rejects sampling params)",
                    name, config["temperature"],
                )
            else:
                gen_config_kwargs["temperature"] = config["temperature"]
        if "max_tokens" in config:
            gen_config_kwargs["max_output_tokens"] = config["max_tokens"]
        if "max_output_tokens" in config:
            gen_config_kwargs["max_output_tokens"] = config["max_output_tokens"]
        if claude_no_sampling:
            cap = gen_config_kwargs.get("max_output_tokens")
            if cap is not None and cap < _CLAUDE_THINKING_MIN_OUTPUT_TOKENS:
                gen_config_kwargs["max_output_tokens"] = _CLAUDE_THINKING_MIN_OUTPUT_TOKENS
                logger.info(
                    "Agent '%s': raising max_output_tokens %s -> %s (adaptive thinking headroom)",
                    name, cap, _CLAUDE_THINKING_MIN_OUTPUT_TOKENS,
                )
        if gen_config_kwargs:
            agent.generate_content_config = types.GenerateContentConfig(**gen_config_kwargs)
            logger.info(f"Agent '{name}' config: {gen_config_kwargs}")

    logger.info(f"ADK agent '{name}' created successfully")
    return agent


def create_sequential_workflow(
    name: str,
    sub_agents: List[LlmAgent],
    description: Optional[str] = None
):
    """
    Create a SequentialAgent workflow.

    SequentialAgent executes sub-agents in order, passing context between them.

    Args:
        name: Workflow name
        sub_agents: List of agents to execute sequentially
        description: Workflow description

    Returns:
        SequentialAgent instance

    Example:
        >>> research_and_report = create_sequential_workflow(
        ...     name="research_pipeline",
        ...     sub_agents=[researcher_agent, scribe_agent, mailer_agent],
        ...     description="Research topic, create document, send email"
        ... )
    """
    from google.adk.agents import SequentialAgent

    return SequentialAgent(
        name=name,
        sub_agents=sub_agents,
        description=description or f"Sequential workflow: {name}"
    )


def create_parallel_workflow(
    name: str,
    sub_agents: List[LlmAgent],
    description: Optional[str] = None,
    aggregation_mode: str = "concatenate"
):
    """
    Create a ParallelAgent workflow.

    ParallelAgent executes sub-agents concurrently and aggregates results.

    Args:
        name: Workflow name
        sub_agents: List of agents to execute in parallel
        description: Workflow description
        aggregation_mode: How to combine results ("concatenate" or "summarize")

    Returns:
        ParallelAgent instance

    Example:
        >>> multi_source_research = create_parallel_workflow(
        ...     name="parallel_research",
        ...     sub_agents=[web_researcher, youtube_researcher, news_researcher],
        ...     aggregation_mode="summarize"
        ... )
    """
    from google.adk.agents import ParallelAgent

    return ParallelAgent(
        name=name,
        sub_agents=sub_agents,
        description=description or f"Parallel workflow: {name}",
        aggregation_mode=aggregation_mode
    )


def create_loop_workflow(
    name: str,
    sub_agents: List[LlmAgent],
    max_iterations: int = 5,
    termination_condition=None,
    description: Optional[str] = None
):
    """
    Create a LoopAgent workflow for iterative improvement.

    LoopAgent executes sub-agents repeatedly until termination condition is met.

    Args:
        name: Workflow name
        sub_agents: List of agents to execute in loop (typically 2: generator + critic)
        max_iterations: Maximum number of iterations
        termination_condition: Callable that takes InvocationContext and returns bool
        description: Workflow description

    Returns:
        LoopAgent instance

    Example:
        >>> def is_approved(ctx):
        ...     return "APPROVED" in ctx.state.get("review_status", "")
        >>>
        >>> iterative_writer = create_loop_workflow(
        ...     name="iterative_document",
        ...     sub_agents=[writer_agent, critic_agent],
        ...     max_iterations=5,
        ...     termination_condition=is_approved
        ... )
    """
    from google.adk.agents import LoopAgent

    return LoopAgent(
        name=name,
        sub_agents=sub_agents,
        max_iterations=max_iterations,
        termination_condition=termination_condition,
        description=description or f"Loop workflow: {name}"
    )
