"""
Plan-Execute orchestration layer.

Problem this solves: the LLM Smart Orchestrator must chain several AgentTools in a
single run. On long multi-step requests it is non-deterministic — after a big
worker result (e.g. a long research report) the model sometimes "feels done" and
stops, so the rest of the chain (rolodex -> mailer) never runs.

This layer makes multi-step requests deterministic:

  1. PLAN   - a lightweight planner LLM decomposes the request into an ordered list
              of steps ({id, agent, task, use_results}).
  2. EXECUTE - a plain Python loop runs each step as its OWN isolated agent call
              (fresh session = clean context, no bloat). Each result is saved to a
              ledger; only the explicitly referenced upstream results are injected
              into a later step.
  3. TRACK  - after every step we record done/failed. If a step fails we STOP and
              report what succeeded; we never feed an empty result downstream.
  4. SUMMARIZE - a final LLM pass turns the ledger into one natural answer in the
              user's language (preserving [IMAGE:...] media tags).

It is intentionally narrow: the planner only takes over for genuine multi-agent
chains. Single actions, fiscalization, confirmations and ambiguous requests get
``multi_step=false`` and fall back to the battle-tested Smart Orchestrator, so this
path can only help the failing case — it never degrades the working ones.
"""

import os
import unicodedata
import re
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from google.genai import types

from agents.adk_agents.adk_agent_factory import (
    claude_safe_generation_kwargs,
    create_adk_agent,
)
from agents.adk_agents.runner_utils import run_agent_simple
from config.deployment_config import callable_worker_agents

logger = logging.getLogger(__name__)

FLASH_MODEL = os.getenv("FLASH_MODEL", "gemini-3.5-flash")

# Sentinel the empty-result guardrail (control_callbacks) injects on failure, plus
# the no-text fallback string. Either means the step did not really produce output.
_FAILURE_MARKERS = ("⚠️ TOOL FAILURE", "No response generated")


# ---------------------------------------------------------------------------
# Planner / summarizer agents
# ---------------------------------------------------------------------------

def _build_available_agents(worker_agents: List[Any]) -> str:
    """Render the worker list the planner is allowed to choose from.

    Filtered here as well as in run_plan_execute, because this is the single
    place the list is rendered: create_workflow_planner is public, and a caller
    that skips run_plan_execute must not be able to advertise an agent no
    execution path can act on.
    """
    parts = []
    for agent in callable_worker_agents(worker_agents):
        name = getattr(agent, "name", "unknown")
        desc = getattr(agent, "description", "") or ""
        parts.append(f"- **{name}**: {desc}")
    return "\n".join(parts) if parts else "(no agents available)"


def create_workflow_planner(
    worker_agents: List[Any],
    model: str = FLASH_MODEL,
    user_timezone: str = "Europe/Zagreb",
):
    """Create the planner agent that emits a JSON workflow plan (no tools)."""
    instruction_file = os.path.join(
        os.path.dirname(__file__), "..", "orchestrator", "planner_instructions.md"
    )
    try:
        with open(instruction_file, "r", encoding="utf-8") as f:
            instruction = f.read()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Failed to load planner instructions: {e}")
        instruction = (
            "Output strict JSON: {\"multi_step\": false, \"language\": \"hr\", "
            "\"reason\": \"fallback\", \"steps\": []}"
        )

    instruction = instruction.replace(
        "{AVAILABLE_AGENTS}", _build_available_agents(worker_agents)
    )
    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    planner = create_adk_agent(
        name="workflow_planner",
        model=model,
        description="Decomposes a user request into an ordered multi-agent plan (JSON).",
        instruction=instruction,
        load_instruction_from_file=False,
    )
    planner.generate_content_config = types.GenerateContentConfig(
        **claude_safe_generation_kwargs(
            "workflow_planner",
            model,
            temperature=0.0,  # deterministic planning
            max_output_tokens=2048,
        )
    )
    return planner


_SUMMARIZER_INSTRUCTION = """You write the FINAL answer to the user after a
multi-step workflow has run. You receive the original request and the result of
each step. Produce ONE natural, conversational reply in the user's language.

Rules:
- Respond in the SAME language as the original request (hr -> Croatian, en -> English).
- Be natural and human, like a helpful assistant. NO robotic markers like
  "[Completed]", "[Završeno]", "[OK]", "[Task]".
- For a multi-step success, briefly say what was done (a short natural list is fine).
- If a step FAILED, be direct about what succeeded and what did not, and suggest
  the next move (e.g. ask for the missing email).
- PRESERVE media tags EXACTLY: if a result contains an `[IMAGE:/api/media/...]`
  tag, copy it verbatim into your reply. Never describe or alter it.
- Do not invent results that are not in the step outputs.
"""


def create_workflow_summarizer(model: str = FLASH_MODEL):
    """Create the agent that turns the ledger into a final user-facing answer."""
    summarizer = create_adk_agent(
        name="workflow_summarizer",
        model=model,
        description="Summarizes multi-step workflow results into one natural reply.",
        instruction=_SUMMARIZER_INSTRUCTION,
        load_instruction_from_file=False,
    )
    summarizer.generate_content_config = types.GenerateContentConfig(
        **claude_safe_generation_kwargs(
            "workflow_summarizer",
            model,
            temperature=0.4,
            max_output_tokens=4096,
        )
    )
    return summarizer


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

# What a step can come back as. The point is that each of these is decided by
# something the code observed, not by reading the agent's prose:
#
#   COMPLETED           the worker answered and nothing stopped it
#   NEEDS_CONFIRMATION  the approval gate held an action inside this step
#   FAILED              it raised, or came back empty
#   UNKNOWN             it broke in a way that may have happened anyway
#
# Before this, everything except "empty" counted as done, so a worker replying
# "trebam tvoju potvrdu prije slanja" was recorded as a finished step and the
# next one carried on as if the mail had gone.
COMPLETED = "completed"
NEEDS_CONFIRMATION = "needs_confirmation"
FAILED = "failed"
UNKNOWN = "unknown"


@dataclass
class StepOutcome:
    """One step's result, with the status kept apart from the prose."""

    status: str
    text: str = ""
    detail: str = ""
    pending: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED


@dataclass
class WorkflowLedger:
    """Side store for step results so the main context never bloats."""

    language: str = "hr"
    plan: List[Dict[str, Any]] = field(default_factory=list)
    results: Dict[int, Dict[str, Any]] = field(default_factory=dict)  # id -> {agent, task, result}
    done: List[int] = field(default_factory=list)
    failure: Optional[Dict[str, Any]] = None  # {id, agent, reason}
    waiting: Optional[Dict[str, Any]] = None  # {id, agent, actions}

    def record(self, step: Dict[str, Any], result: str) -> None:
        sid = step["id"]
        self.results[sid] = {"agent": step["agent"], "task": step["task"], "result": result}
        self.done.append(sid)

    def context_for(self, step: Dict[str, Any]) -> str:
        """Render only the upstream results this step asked for."""
        blocks = []
        for ref in step.get("use_results", []) or []:
            entry = self.results.get(ref)
            if entry:
                blocks.append(
                    f"### Korak {ref} ({entry['agent']}):\n{entry['result']}"
                )
        if not blocks:
            return ""
        return (
            "\n\n## PODACI IZ PRETHODNIH KORAKA (koristi ih, ne izmišljaj)\n"
            + "\n\n".join(blocks)
        )

    def summary_payload(self, user_message: str) -> str:
        lines = [
            f"ORIGINALNI ZAHTJEV: {user_message}",
            f"JEZIK ODGOVORA: {self.language}",
            "",
            "REZULTATI KORAKA:",
        ]
        if self.waiting or self.failure:
            # Telling the user to "confirm and ask again" would replay every
            # step that already finished — including the writes. The summary
            # has to name what is done before it names what is missing.
            lines.insert(2, (
                "VAŽNO: lanac je stao prije kraja. U odgovoru NAJPRIJE navedi "
                "što je već izvršeno (ti su koraci gotovi i NE treba ih "
                "ponavljati), pa tek onda što je ostalo i što ti treba od "
                "korisnika. Nemoj tražiti da ponovi cijeli zahtjev."
            ))
        for step in self.plan:
            sid = step["id"]
            entry = self.results.get(sid)
            if entry:
                lines.append(f"\n[Korak {sid} - {step['agent']}] OK\n{entry['result']}")
            elif self.waiting and self.waiting["id"] == sid:
                lines.append(
                    f"\n[Korak {sid} - {step['agent']}] ČEKA POTVRDU: "
                    f"{', '.join(self.waiting['actions']) or 'radnja traži potvrdu'}"
                )
            elif self.failure and self.failure["id"] == sid:
                lines.append(
                    f"\n[Korak {sid} - {step['agent']}] NEUSPJEH: {self.failure['reason']}"
                )
            else:
                lines.append(f"\n[Korak {sid} - {step['agent']}] NIJE IZVRŠEN")
        lines.append("\nNapiši prirodan završni odgovor korisniku.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of the planner's text, tolerating fences."""
    if not text:
        return None
    cleaned = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        # Otherwise grab the outermost {...}.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[PLAN] Could not parse planner JSON: {e}")
        return None


def _validate_plan(plan: dict, valid_agents: set) -> Optional[List[Dict[str, Any]]]:
    """Return a clean step list, or None if the plan is unusable / single-step."""
    if not isinstance(plan, dict):
        return None
    if not plan.get("multi_step"):
        return None
    steps = plan.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        # Fewer than 2 steps is not a chain — let the orchestrator handle it.
        return None

    try:
        max_steps = int(os.getenv("PLAN_EXECUTE_MAX_STEPS", "8"))
    except ValueError:
        max_steps = 8
    if max_steps > 0 and len(steps) > max_steps:
        # Each step is its own agent run. A planner that emits thirty of them
        # is not describing this request any more.
        logger.warning("[PLAN] Rejecting a %d-step plan (max %d)", len(steps), max_steps)
        return None

    clean: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for i, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            return None
        agent = raw.get("agent")
        task = raw.get("task")
        if agent not in valid_agents or not isinstance(task, str) or not task.strip():
            logger.warning(f"[PLAN] Invalid step {i}: agent={agent!r}")
            return None

        step_id = int(raw.get("id", i))
        if step_id in seen_ids:
            # Two steps with the same id write to the same ledger slot, so the
            # second silently replaces the first and the summary prints it twice.
            logger.warning("[PLAN] Duplicate step id %s", step_id)
            return None

        refs = [int(r) for r in (raw.get("use_results") or []) if str(r).isdigit()]
        for ref in refs:
            if ref not in seen_ids:
                # A reference forward or to nothing renders as an empty context
                # block, and the step runs on its task alone — the exact
                # "never feed an empty result downstream" failure this module
                # exists to prevent, arriving quietly.
                logger.warning(
                    "[PLAN] Step %s uses result %s, which is not an earlier step",
                    step_id, ref,
                )
                return None

        seen_ids.add(step_id)
        clean.append(
            {
                "id": step_id,
                "agent": agent,
                "task": task.strip(),
                "use_results": refs,
            }
        )
    return clean


def _looks_failed(result: str) -> bool:
    if not result or not result.strip():
        return True
    head = result.strip()[:80]
    return any(marker in head for marker in _FAILURE_MARKERS)


# What a tool says about itself, mapped to what it means for the step.
_UNKNOWN_TOOL_STATUSES = {"unknown", "unconfirmed", "timeout"}
_FAILED_TOOL_STATUSES = {"failed", "error", "aborted", "not_permitted"}


def _classify_step(
    result: str,
    pending: List[str],
    outcomes: Optional[List[tuple]] = None,
) -> StepOutcome:
    """What actually happened in this step.

    Two kinds of evidence, both recorded by code rather than read out of the
    worker's prose:

    * `pending` — actions the approval gate held while the step ran;
    * `outcomes` — statuses the tools reported in their own results.

    That distinction is the whole point. The final text is written by a model,
    and a tool result saying `{"status": "unknown"}` becomes an ordinary
    sentence by the time it gets there — which is how a write nobody could
    confirm used to count as a finished step.

    `completed` is still the absence of trouble, but the trouble it now has to
    be absent of includes what the tools themselves said.
    """
    outcomes = outcomes or []

    unknown = [name for name, status in outcomes
               if status.lower() in _UNKNOWN_TOOL_STATUSES]
    if unknown:
        return StepOutcome(
            status=UNKNOWN,
            text=result or "",
            detail=(
                "alat nije mogao potvrditi ishod: " + ", ".join(sorted(set(unknown)))
            ),
        )

    if pending:
        return StepOutcome(
            status=NEEDS_CONFIRMATION,
            text=result or "",
            detail="radnja traži potvrdu korisnika",
            pending=list(pending),
        )

    failed = [name for name, status in outcomes
              if status.lower() in _FAILED_TOOL_STATUSES]
    if failed:
        return StepOutcome(
            status=FAILED,
            text=result or "",
            detail="alat je prijavio neuspjeh: " + ", ".join(sorted(set(failed))),
        )

    if _looks_failed(result):
        return StepOutcome(
            status=FAILED,
            text=result or "",
            detail="agent je vratio prazan rezultat — korak nije dovršen",
        )
    return StepOutcome(status=COMPLETED, text=result)


def _classify_error(error: Exception) -> StepOutcome:
    """A raised step: did it definitely not happen, or is that unknown?"""
    try:
        from tools.resilience.retry_handler import is_retryable_error
        unknown = bool(is_retryable_error(error))
    except Exception:  # pragma: no cover - defensive
        unknown = False
    if unknown:
        return StepOutcome(
            status=UNKNOWN,
            detail=f"veza je pukla, ne znam je li korak izvršen: {error}",
        )
    return StepOutcome(status=FAILED, detail=f"greška: {error}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# --- Cheap pre-filter: is it even worth asking the planner? -----------------
#
# The planner is a full Sonnet call (~2.7k input tokens, ~4 s) and it ran ahead
# of every orchestrated request, including the ones it immediately declared
# single-step. "Upali svjetlo" does not need a plan.
#
# The voice path prepends a persona block before the transcript, so the user's
# own words have to be recovered before anything is counted.
_VOICE_PROFILE_END = "[/VOICE_ASSISTANT_PROFILE]"
_VOICE_TRANSCRIPT_PREFIXES = (
    "Korisnik je rekao:", "User request:", "User question:",
)

# Joiners that actually chain one action onto another.
_CHAIN_MARKERS = (
    " i posalji", " i poslji", " i napravi", " i spremi", " i dodaj", " i javi",
    " i upisi", " i posalje", " pa posalji", " pa napravi", " pa mi", " pa ga",
    " zatim", " te mi", " te ga", " nakon toga", " onda ",
    " and send", " and create", " and email", " and add", " then ",
)


def _user_text(message: str) -> str:
    """The user's own words, with any voice persona preamble stripped."""
    _, sep, tail = message.partition(_VOICE_PROFILE_END)
    text = tail if sep else message
    for prefix in _VOICE_TRANSCRIPT_PREFIXES:
        idx = text.find(prefix)
        if idx != -1:
            text = text[idx + len(prefix):]
            break
    return text.strip()


def _looks_multi_step(message: str) -> bool:
    """True when a request is worth a planner call.

    Deliberately generous: a false positive costs one planner call, a false
    negative only means the Smart Orchestrator handles the request itself,
    which is exactly what happens today when the planner says "single step".
    """
    try:
        threshold = int(os.getenv("PLAN_EXECUTE_MIN_WORDS", "12"))
    except ValueError:
        threshold = 12
    if threshold <= 0:  # opt out of the shortcut entirely
        return True

    text = _user_text(message)
    # Fold Croatian diacritics so "pošalji" matches the ASCII marker list,
    # and collapse whitespace so a line break cannot hide a joiner.
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = folded.encode("ascii", "ignore").decode("ascii")
    lowered = " " + " ".join(folded.split()) + " "
    if any(marker in lowered for marker in _CHAIN_MARKERS):
        return True
    return len(text.split()) >= threshold


async def run_plan_execute(
    user_message: str,
    worker_agents: List[Any],
    *,
    fallback: Callable[[str], Awaitable[str]],
    session_id: str = "default-session",
    user_id: str = "default-user",
    planner_model: str = FLASH_MODEL,
    summarizer_model: str = FLASH_MODEL,
    record_turn: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> str:
    """Run a request via plan-execute, falling back to ``fallback`` when it is not a
    genuine multi-step chain (or if anything goes wrong).

    Args:
        user_message: Raw user request.
        worker_agents: List of worker LlmAgents (the agents the planner may use).
        fallback: Async callable (the existing Smart Orchestrator run) used for
            single-step / special / error cases. MUST be awaitable -> str.
        session_id/user_id: Base identifiers; each step gets its own derived session.
        record_turn: Optional async callable ``(user_message, answer) -> None`` used
            to persist a genuine multi-step turn into the caller's conversation
            session. NOT called on the fallback paths — the fallback (orchestrator)
            already records into its own session — so cross-turn context is kept
            without double-recording.

    Returns:
        Final user-facing answer text.
    """
    from services import run_effects

    if not _looks_multi_step(user_message):
        logger.info("[PLAN] Short single-action request — skipping planner.")
        return await fallback(user_message)

    # The planner advertises these and _validate_plan accepts them, so a worker
    # that must never be invoked has to be filtered out HERE too, not only in
    # the orchestrator's tool list. Same list for both execution paths.
    worker_agents = callable_worker_agents(worker_agents)
    agents_by_name = {getattr(a, "name", ""): a for a in worker_agents}
    valid_agents = set(agents_by_name)

    # --- 1. PLAN ---
    try:
        planner = create_workflow_planner(worker_agents, model=planner_model)
        planner_raw = await run_agent_simple(
            planner,
            user_message,
            session_id=f"{session_id}-planner",
            user_id=user_id,
        )
        plan = _extract_json(planner_raw)
        steps = _validate_plan(plan, valid_agents) if plan else None
    except Exception as e:
        logger.error(f"[PLAN] Planner failed, falling back to orchestrator: {e}")
        return await fallback(user_message)

    if not steps:
        logger.info("[PLAN] Not a multi-step chain — deferring to Smart Orchestrator.")
        return await fallback(user_message)

    ledger = WorkflowLedger(language=(plan.get("language") or "hr"), plan=steps)
    logger.info(
        "[PLAN] Multi-step plan accepted: %s",
        " -> ".join(f"{s['id']}:{s['agent']}" for s in steps),
    )

    # --- 2. EXECUTE (deterministic loop, fresh context per step) ---
    # ensure_run, not start_run: a scheduled job already started a ledger and
    # resetting it would erase what that job needs to decide whether replaying
    # is safe. This only covers the case where nothing upstream tracked.
    run_effects.ensure_run()

    for step in steps:
        sid, agent_name = step["id"], step["agent"]
        agent = agents_by_name[agent_name]
        composed = step["task"] + ledger.context_for(step)

        # Where the ledger stood before this step, so the deltas below belong
        # to this step alone and not to something earlier in the run.
        before = set(run_effects.confirmations_needed())
        before_outcomes = len(run_effects.tool_outcomes())

        logger.info("[STEP %s] -> %s", sid, agent_name)
        try:
            result = await run_agent_simple(
                agent,
                composed,
                session_id=f"{session_id}-step{sid}-{agent_name}",
                user_id=user_id,
            )
        except Exception as e:
            logger.error("[STEP %s] %s raised: %s", sid, agent_name, e)
            outcome = _classify_error(e)
        else:
            pending = [
                a for a in run_effects.confirmations_needed() if a not in before
            ]
            outcome = _classify_step(
                result,
                pending,
                list(run_effects.tool_outcomes()[before_outcomes:]),
            )

        if outcome.status == NEEDS_CONFIRMATION:
            # Stop here rather than carrying on as if it had run. The steps
            # after this one were written expecting it to be done.
            logger.info(
                "[STEP %s] %s is waiting for confirmation: %s",
                sid, agent_name, ", ".join(outcome.pending),
            )
            ledger.waiting = {
                "id": sid, "agent": agent_name, "actions": outcome.pending,
            }
            break

        if not outcome.ok:
            # UNKNOWN stops the chain for the same reason FAILED does: the
            # steps after this one were written assuming it finished, and
            # "maybe it did" is not a foundation to send a mail on.
            logger.error("[STEP %s] %s -> %s", sid, agent_name, outcome.status)
            ledger.failure = {
                "id": sid,
                "agent": agent_name,
                "reason": outcome.detail,
                "status": outcome.status,
            }
            break

        ledger.record(step, outcome.text)
        logger.info("[STEP %s] %s OK (%d chars)", sid, agent_name, len(outcome.text))

    # --- 3. SUMMARIZE ---
    answer: Optional[str] = None
    try:
        summarizer = create_workflow_summarizer(model=summarizer_model)
        final = await run_agent_simple(
            summarizer,
            ledger.summary_payload(user_message),
            session_id=f"{session_id}-summary",
            user_id=user_id,
        )
        if final and final.strip():
            answer = final
    except Exception as e:
        logger.error(f"[SUMMARY] Summarizer failed: {e}")

    # Summarizer fallback: stitch the ledger together plainly so we never return empty.
    if answer is None:
        if ledger.failure:
            last = ledger.results.get(ledger.done[-1]) if ledger.done else None
            done_note = f" Zadnji uspješan korak: {last['agent']}." if last else ""
            answer = (
                f"Dio zadatka je odrađen, ali korak {ledger.failure['id']} "
                f"({ledger.failure['agent']}) nije uspio: {ledger.failure['reason']}.{done_note}"
            )
        else:
            last = ledger.results.get(ledger.done[-1]) if ledger.done else None
            answer = last["result"] if last else "No response generated"

    # Persist this turn into the caller's conversation session so a follow-up
    # (e.g. answering a clarifying question this workflow asked) keeps context.
    if record_turn is not None:
        try:
            await record_turn(user_message, answer)
        except Exception as e:
            logger.warning(f"[PLAN] Could not record turn into session: {e}")

    return answer
