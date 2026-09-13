"""
A step is finished when something observed says so, not when the prose reads well.

`_looks_failed` accepted anything non-empty, so a worker replying "trebam tvoju
potvrdu prije slanja" was recorded as a completed step and the next one carried
on as though the mail had gone out. The sentinel it also looked for,
`⚠️ TOOL FAILURE`, is injected by a callback wired onto the orchestrator — and
plan-execute calls workers directly, so on this path it could never appear.

Now each step comes back with a status decided by evidence:

* `needs_confirmation` — the approval gate held an action while the step ran,
  read from `run_effects`, which the gate writes to. A fact, not a phrase.
* `failed` — the step raised, or returned nothing.
* `unknown` — it broke in a way that may have happened anyway.
* `completed` — none of the above.

The plan itself is validated first, because a result contract means nothing if
a step can claim a result no earlier step produced.

No LLM: the planner and the workers are stubbed.

Run with:
    pytest tests/unit/test_plan_execute_contract.py -v
"""

import json

import pytest

from agents.adk_agents import plan_execute
from agents.adk_agents.plan_execute import (
    COMPLETED,
    FAILED,
    NEEDS_CONFIRMATION,
    UNKNOWN,
    StepOutcome,
    _classify_error,
    _classify_step,
    _validate_plan,
)
from services import run_effects

AGENTS = {"scribe", "mailer", "analyst"}


def _plan(*steps, multi_step=True):
    return {"multi_step": multi_step, "steps": list(steps)}


def _step(step_id, agent="scribe", task="napravi nešto", use_results=None):
    out = {"id": step_id, "agent": agent, "task": task}
    if use_results is not None:
        out["use_results"] = use_results
    return out


# --- the plan has to hold together ------------------------------------------

class TestPlanValidation:

    def test_a_sound_plan_is_accepted(self):
        steps = _validate_plan(
            _plan(_step(1), _step(2, "mailer", use_results=[1])), AGENTS
        )
        assert [s["id"] for s in steps] == [1, 2]
        assert steps[1]["use_results"] == [1]

    def test_duplicate_step_ids_are_rejected(self):
        # Both write to the same ledger slot, so the second silently replaces
        # the first and the summary prints it twice.
        assert _validate_plan(_plan(_step(2), _step(2, "mailer")), AGENTS) is None

    def test_a_forward_reference_is_rejected(self):
        # It renders as an empty context block and the step runs on its task
        # alone — the "never feed an empty result downstream" failure this
        # module exists to prevent, arriving quietly.
        assert _validate_plan(
            _plan(_step(1, use_results=[2]), _step(2, "mailer")), AGENTS
        ) is None

    def test_a_reference_to_nothing_is_rejected(self):
        assert _validate_plan(
            _plan(_step(1), _step(2, "mailer", use_results=[7])), AGENTS
        ) is None

    def test_too_many_steps_are_rejected(self, monkeypatch):
        monkeypatch.setenv("PLAN_EXECUTE_MAX_STEPS", "3")
        many = _plan(*[_step(n) for n in range(1, 6)])
        assert _validate_plan(many, AGENTS) is None

    def test_the_cap_can_be_lifted(self, monkeypatch):
        monkeypatch.setenv("PLAN_EXECUTE_MAX_STEPS", "0")
        many = _plan(*[_step(n) for n in range(1, 12)])
        assert _validate_plan(many, AGENTS) is not None


# --- the status comes from evidence ------------------------------------------

class TestStepClassification:

    def test_a_plain_answer_is_completed(self):
        assert _classify_step("Dokument je napravljen.", []).status == COMPLETED

    def test_an_empty_answer_is_failed(self):
        assert _classify_step("   ", []).status == FAILED

    def test_a_held_action_is_needs_confirmation(self):
        outcome = _classify_step(
            "Pripremio sam mail.", ["gmail_send_message"]
        )
        assert outcome.status == NEEDS_CONFIRMATION
        assert outcome.pending == ["gmail_send_message"]

    def test_prose_alone_never_decides(self):
        # The worker says it needs confirmation, but the gate held nothing —
        # perhaps it is describing what it did. Guessing from the sentence is
        # exactly what this replaced.
        assert _classify_step(
            "Trebam tvoju potvrdu prije slanja.", []
        ).status == COMPLETED

    def test_a_rejection_is_failed(self):
        assert _classify_error(ValueError("400 Bad Request")).status == FAILED

    def test_a_lost_connection_is_unknown(self):
        outcome = _classify_error(TimeoutError("The read operation timed out"))
        assert outcome.status == UNKNOWN
        assert "ne znam" in outcome.detail


# --- the chain stops where it should ----------------------------------------

class TestTheChainStops:

    @pytest.fixture(autouse=True)
    def clean(self):
        run_effects.clear()
        yield
        run_effects.clear()

    async def _run(self, monkeypatch, behaviours):
        """Drive the executor with a stub planner, workers and summarizer."""
        calls = []

        class _Agent:
            def __init__(self, name):
                self.name = name

        agents = [_Agent(name) for name in ("scribe", "mailer")]

        plan_json = json.dumps({
            "multi_step": True,
            "language": "hr",
            "steps": [
                {"id": 1, "agent": "scribe", "task": "napravi dokument"},
                {"id": 2, "agent": "mailer", "task": "posalji ga",
                 "use_results": [1]},
            ],
        })

        async def fake_run(agent, message, **kwargs):
            name = getattr(agent, "name", "?")
            if name == "workflow_planner":
                return plan_json
            if name == "workflow_summarizer":
                # The whole payload: the step lines are what these tests
                # assert on, and truncating hid them behind the header.
                return "SAZETAK: " + message
            calls.append(name)
            return behaviours[name]()

        monkeypatch.setattr(plan_execute, "run_agent_simple", fake_run)
        monkeypatch.setattr(
            plan_execute, "create_workflow_planner",
            lambda *a, **k: _Agent("workflow_planner"),
        )
        monkeypatch.setattr(
            plan_execute, "create_workflow_summarizer",
            lambda *a, **k: _Agent("workflow_summarizer"),
        )
        return agents, calls

    @pytest.mark.asyncio
    async def test_a_step_waiting_for_confirmation_stops_the_chain(
        self, monkeypatch
    ):
        run_effects.start_run()

        def scribe():
            run_effects.note_needs_confirmation("drive_share_file")
            return "Napravio sam dokument."

        agents, calls = await self._run(
            monkeypatch, {"scribe": scribe, "mailer": lambda: "Poslano."}
        )

        answer = await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="s1",
            user_id="u1",
        )

        # The mailer step was written assuming the document exists and is
        # shared. It must not run on the strength of a step that stopped.
        assert calls == ["scribe"]
        assert "ČEKA POTVRDU" in answer

    @pytest.mark.asyncio
    async def test_a_completed_chain_runs_both_steps(self, monkeypatch):
        run_effects.start_run()

        agents, calls = await self._run(
            monkeypatch,
            {"scribe": lambda: "Dokument spreman.", "mailer": lambda: "Poslano."},
        )

        await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="s1",
            user_id="u1",
        )

        assert calls == ["scribe", "mailer"]

    @pytest.mark.asyncio
    async def test_a_confirmation_from_an_earlier_step_is_not_reattributed(
        self, monkeypatch
    ):
        # Something earlier in the run needed confirmation. That belongs to
        # the earlier thing, not to every step that follows it.
        run_effects.start_run()
        run_effects.note_needs_confirmation("calendar_delete_event")

        agents, calls = await self._run(
            monkeypatch,
            {"scribe": lambda: "Dokument spreman.", "mailer": lambda: "Poslano."},
        )

        await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="s1",
            user_id="u1",
        )

        assert calls == ["scribe", "mailer"]


async def _never():  # pragma: no cover - the fallback must not be reached
    raise AssertionError("fallback was called")


class TestStepOutcome:

    def test_only_completed_counts_as_ok(self):
        assert StepOutcome(status=COMPLETED).ok is True
        for status in (NEEDS_CONFIRMATION, FAILED, UNKNOWN):
            assert StepOutcome(status=status).ok is False


# --- through the real path, with nothing primed ------------------------------

class TestTheRealPathTracksByItself:
    """The tests above prime `run_effects.start_run()` themselves.

    That is exactly the trap: they proved the classifier worked on a ledger
    the real path never opened. `start_run()` was called only in the
    scheduler, so in ordinary chat and in plan-execute the ledger was None and
    every recorder was inert — a step that stopped for a confirmation still
    read as completed, and the next one ran.

    Nothing here touches `run_effects` before the code does.
    """

    @pytest.fixture(autouse=True)
    def clean(self):
        from services import approval_gate, approvals

        run_effects.clear()
        approvals.reset()
        approvals.set_session("plan-session")
        approval_gate._holds_this_run.clear()
        yield
        run_effects.clear()
        approvals.reset()
        approval_gate._holds_this_run.clear()

    @pytest.mark.asyncio
    async def test_a_gated_action_stops_the_chain_without_priming(
        self, monkeypatch
    ):
        from services.approval_gate import approval_before_tool

        class _Tool:
            def __init__(self, name):
                self.name = name

        def scribe():
            # The real recorder: the gate holds, and the gate is what writes
            # to the ledger.
            held = approval_before_tool(
                tool=_Tool("calendar_delete_event"), args={"event_id": "E1"}
            )
            assert held["status"] == "needs_confirmation"
            return "Pripremio sam brisanje termina."

        agents, calls = await TestTheChainStops()._run(
            monkeypatch, {"scribe": scribe, "mailer": lambda: "Poslano."}
        )

        answer = await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="plan-session",
            user_id="u1",
        )

        assert calls == ["scribe"]
        assert "ČEKA POTVRDU" in answer

    @pytest.mark.asyncio
    async def test_a_tool_reporting_unknown_stops_the_chain(self, monkeypatch):
        def scribe():
            # What an HA command or a Google write says when its answer was
            # lost. By the time it reaches the step, the text has swallowed it.
            run_effects.note_tool_outcome("sheets_append_values", "unknown")
            return "Dodao sam redak u tablicu."

        agents, calls = await TestTheChainStops()._run(
            monkeypatch, {"scribe": scribe, "mailer": lambda: "Poslano."}
        )

        answer = await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="plan-session",
            user_id="u1",
        )

        # "Maybe it worked" is not a foundation to send a mail on.
        assert calls == ["scribe"]
        assert "NEUSPJEH" in answer

    @pytest.mark.asyncio
    async def test_an_ordinary_chain_is_unaffected(self, monkeypatch):
        agents, calls = await TestTheChainStops()._run(
            monkeypatch,
            {"scribe": lambda: "Dokument spreman.", "mailer": lambda: "Poslano."},
        )

        await plan_execute.run_plan_execute(
            "napravi dokument pa ga posalji",
            agents,
            fallback=lambda m: _never(),
            session_id="plan-session",
            user_id="u1",
        )

        assert calls == ["scribe", "mailer"]


class TestATurnOpensTheLedger:

    @pytest.mark.asyncio
    async def test_prepare_turn_starts_tracking(self, monkeypatch):
        from types import SimpleNamespace

        from interfaces.base_interface import BaseInterface

        class _Iface(BaseInterface):
            def __init__(self):
                super().__init__(session_prefix="test")
                self.system = SimpleNamespace(
                    orchestrator=object(),
                    orchestrator_helper=SimpleNamespace(
                        session_id="s1", user_id="ana", session_service=None
                    ),
                    session_id=None,
                    user_id=None,
                )

            def initialize_system(self):
                pass

            async def start(self):
                pass

            async def stop(self):
                pass

            def format_response(self, response):
                return response

        run_effects.clear()
        assert run_effects.tool_calls() == ()

        await _Iface()._prepare_turn("ana", "pozdrav", "s1")

        # Inert before, live after: every recorder downstream depends on this.
        run_effects.note_tool_call("gmail_send_message")
        assert run_effects.anything_happened() is True
        run_effects.clear()
