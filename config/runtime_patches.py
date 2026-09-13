"""Runtime patches for known third-party ADK issues."""

from __future__ import annotations

import os
import logging

from config.deployment_config import is_adk_telemetry_disabled

logger = logging.getLogger(__name__)

_PATCHED = False

# Marker an agent prompt can put before its volatile tail (a clock, per-run
# state) so prompt caching keeps a breakpoint at the end of the stable part
# instead of over the whole thing. Everything before it is cached; everything
# after is sent fresh. Prompts without it are cached as a single block.
CACHE_BREAK = "<!-- CACHE_BREAK -->"


def _cache_conversation_enabled() -> bool:
    return os.getenv("CLAUDE_CACHE_CONVERSATION", "true").lower() in (
        "1", "true", "yes", "on"
    )


def cache_blocks_for(content: str):
    """Split the system prompt at CACHE_BREAK so volatile text stays outside the cache.

    Anthropic caching is a prefix match: the breakpoint has to sit at the end
    of the *stable* part. A prompt that ends in something that changes every
    minute and carries one breakpoint over the whole thing pays the write
    premium on every request and never reads anything back.

    Prompts without the marker are one block, as before.
    """
    static, sep, volatile = content.partition(CACHE_BREAK)
    if not sep or not static.strip():
        return [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]

    blocks = [{
        "type": "text",
        "text": static,
        "cache_control": {"type": "ephemeral"},
    }]
    if volatile.strip():
        # Deliberately no cache_control: this is the part that changes.
        blocks.append({"type": "text", "text": volatile})
    return blocks


def mark_conversation_prefix(messages) -> bool:
    """Put a cache breakpoint at the end of the conversation so far.

    Caching the system prompt alone is enough for a one-shot agent and useless
    for a ReAct loop. The researcher makes eight calls, each resending the whole
    history — every search result and every scraped page — so the tokens that
    dominate the bill are the ones nothing was caching. Measured 2026-09-03: one
    research turn sent 1,176,439 input tokens across eight calls and read 43,057
    from cache, at $6.14.

    A breakpoint on the last message makes each call read everything the
    previous calls accumulated and write only what this turn added.

    Returns True when a breakpoint was placed.
    """
    conversation = [
        m for m in (messages or [])
        if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None))
        not in ("system", "developer")
    ]
    if len(conversation) < 2:
        # One user turn has nothing accumulated behind it; a breakpoint there
        # only pays the write premium.
        return False

    last = conversation[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)

    if isinstance(content, str):
        if not content.strip():
            return False
        block = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        if isinstance(last, dict):
            last["content"] = block
        else:
            last.content = block
        return True

    if isinstance(content, list) and content:
        # Mark the final block; anything earlier would cut the prefix short.
        tail = content[-1]
        if isinstance(tail, dict) and tail.get("type"):
            tail["cache_control"] = {"type": "ephemeral"}
            return True

    return False


def apply_runtime_patches() -> None:
    """Apply idempotent runtime patches for unstable optional telemetry."""
    global _PATCHED
    if _PATCHED:
        return

    if is_adk_telemetry_disabled():
        _disable_adk_telemetry()

    if os.getenv("CLAUDE_PROMPT_CACHE", "true").lower() in ("1", "true", "yes", "on"):
        _enable_litellm_prompt_cache()

    if os.getenv("LLM_PROVIDER", "gemini").lower() == "anthropic":
        _enable_litellm_drop_params()

    _PATCHED = True


def _enable_litellm_drop_params() -> None:
    """Drop sampling params the target Claude model doesn't accept.

    Claude Sonnet 5 / Opus 4.7+ reject non-default temperature/top_p/top_k
    (LiteLLM raises UnsupportedParamsError client-side and retries pointlessly).
    The agent factory strips these for agents built through create_adk_agent,
    but some call sites set generate_content_config directly on the agent
    (smart_orchestrator, plan_execute planner/summarizer). drop_params makes
    LiteLLM silently drop whatever the model doesn't support — the safety net
    for every current and future call site.
    """
    try:
        import litellm
    except Exception as exc:  # litellm not installed
        logger.debug("LiteLLM drop_params patch skipped: %s", exc)
        return
    litellm.drop_params = True
    logger.info("LiteLLM drop_params enabled (unsupported sampling params are dropped)")


def _enable_litellm_prompt_cache() -> None:
    """Mark the system prompt for Anthropic prompt caching on LiteLLM calls.

    ADK's LiteLlm wrapper inserts the agent instruction as a plain-string
    system/developer message. Anthropic only caches a content *block* carrying
    ``cache_control``. We wrap ``_get_completion_inputs`` to convert that string
    into a cache-marked text block, so the large static agent instructions are
    cached (~90% cheaper on subsequent calls within the TTL).

    Only affects LiteLLM (Claude) calls — the Gemini path never calls this
    function. No-ops if litellm/ADK LiteLlm is unavailable. Cache hits show up
    in usage as cached tokens (the /tokens "cached" column).
    """
    try:
        import google.adk.models.lite_llm as ll
    except Exception as exc:  # litellm not installed, or import error
        logger.debug("LiteLLM prompt-cache patch skipped: %s", exc)
        return

    if getattr(ll, "_prompt_cache_patched", False):
        return

    import inspect

    original = ll._get_completion_inputs

    def _mark(result):
        """Mark the system message content for Anthropic prompt caching, in place."""
        try:
            messages = result[0] if isinstance(result, (tuple, list)) else None
            for msg in messages or []:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
                if role in ("system", "developer"):
                    content = (
                        msg.get("content") if isinstance(msg, dict)
                        else getattr(msg, "content", None)
                    )
                    if isinstance(content, str) and content.strip():
                        block = cache_blocks_for(content)
                        # LiteLLM reads cache_control only from role=="system".
                        if isinstance(msg, dict):
                            msg["content"] = block
                            msg["role"] = "system"
                        else:
                            msg.content = block
                            try:
                                msg.role = "system"
                            except Exception:
                                pass
                    break  # only the (single) system/developer message

            # The system prompt is the small half in an agentic loop.
            if messages and _cache_conversation_enabled():
                mark_conversation_prefix(messages)
        except Exception as exc:  # never break the request over caching
            logger.debug("prompt-cache marking skipped: %s", exc)
        return result

    # ADK versions differ: _get_completion_inputs may be sync or async, and take
    # 1 or 2 positional args. Handle both, passing args straight through.
    if inspect.iscoroutinefunction(original):
        async def _patched(*args, **kwargs):
            return _mark(await original(*args, **kwargs))
    else:
        def _patched(*args, **kwargs):
            return _mark(original(*args, **kwargs))

    ll._get_completion_inputs = _patched
    ll._prompt_cache_patched = True
    logger.info(
        "LiteLLM Anthropic prompt-caching patch applied (async=%s)",
        inspect.iscoroutinefunction(original),
    )


def _disable_adk_telemetry() -> None:
    """Disable ADK tracing hooks that can crash on non-JSON-safe payloads."""
    try:
        import google.adk.telemetry as telemetry

        def _noop(*args, **kwargs):
            return None

        telemetry.trace_call_llm = _noop
        telemetry.trace_send_data = _noop
        telemetry.trace_tool_call = _noop
        telemetry.trace_tool_response = _noop
        logger.info("ADK telemetry tracing disabled by runtime patch")
    except Exception as exc:
        logger.warning("Failed to disable ADK telemetry tracing: %s", exc)
