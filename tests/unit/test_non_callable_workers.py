"""A worker that must not be invoked has to be unreachable on BOTH paths.

voice_qa was removed from the orchestrator's AgentTool list, which closed one
execution path and left the other open: main.py hands run_plan_execute the same
unfiltered self.worker_agents, the planner advertised voice_qa in
{AVAILABLE_AGENTS}, and _validate_plan accepted it as a step. With
USE_PLAN_EXECUTE=true the sentinel could still reach a user as literal text.

The list now lives in one place and both paths filter through it.

The first version of the rejection test proved nothing: it sent a ONE-step
plan, which _validate_plan rejects on length before it ever looks at the
agent, with `request` where the field is `task` and a string where the id goes
through int(). It would have passed for a perfectly legal agent. Every
rejection here is now paired with a control that must be accepted, and the
boundary case drives run_plan_execute itself with a fake planner, counting
which agents actually ran.

Run with:
    pytest tests/unit/test_non_callable_workers.py -v
"""

import asyncio
import json

import pytest
from google.adk.agents import LlmAgent

from agents.adk_agents import plan_execute
from agents.adk_agents.plan_execute import (
    _build_available_agents,
    _validate_plan,
    create_workflow_planner,
)
from agents.adk_agents.smart_orchestrator import create_smart_orchestrator
from config.deployment_config import (
    NON_CALLABLE_WORKER_AGENTS,
    callable_worker_agents,
)


def _worker(name: str) -> LlmAgent:
    return LlmAgent(
        name=name, model="gemini-3.5-flash", description=f"{name} desc", instruction="x"
    )


WORKERS = [_worker("voice_qa"), _worker("mailer"), _worker("scribe")]


class TestTheListItself:
    def test_voice_qa_is_on_it(self):
        assert "voice_qa" in NON_CALLABLE_WORKER_AGENTS

    def test_the_filter_drops_it_and_keeps_the_rest(self):
        names = [a.name for a in callable_worker_agents(WORKERS)]
        assert names == ["mailer", "scribe"]

    def test_the_filter_tolerates_objects_without_a_name(self):
        assert callable_worker_agents([object()])


class TestOrchestratorPath:
    def test_it_is_not_a_tool(self):
        orc = create_smart_orchestrator(model="gemini-3.5-flash", worker_agents=WORKERS)
        assert "voice_qa" not in {getattr(t, "name", "") for t in orc.tools}


class TestPlanExecutePath:
    def test_the_planner_does_not_advertise_it(self):
        rendered = _build_available_agents(WORKERS)
        assert "**mailer**" in rendered
        assert "voice_qa" not in rendered

    def test_the_built_planner_prompt_does_not_either(self):
        planner = create_workflow_planner(WORKERS)
        instruction = planner.instruction
        text = instruction(None) if callable(instruction) else instruction
        assert "voice_qa" not in text


def _plan(*agents):
    """A plan _validate_plan would otherwise accept.

    Shape matters more than it looks: fewer than two steps is rejected before
    the agent is ever examined, the task field is `task` (not `request`), and
    ids go through int(). The first version of this test got all three wrong
    and passed for reasons that had nothing to do with voice_qa.
    """
    return {
        "multi_step": True,
        "language": "hr",
        "steps": [
            {"id": i, "agent": a, "task": f"korak {i}"}
            for i, a in enumerate(agents, start=1)
        ],
    }


class TestValidatorRejectsANonCallableStep:
    VALID = {a.name for a in callable_worker_agents(WORKERS)}

    def test_the_control_plan_is_accepted(self):
        """Without this, the rejection below proves nothing."""
        steps = _validate_plan(_plan("mailer", "scribe"), self.VALID)
        assert steps is not None
        assert [s["agent"] for s in steps] == ["mailer", "scribe"]

    @pytest.mark.parametrize("agent", sorted(NON_CALLABLE_WORKER_AGENTS))
    def test_swapping_one_agent_rejects_it(self, agent):
        """Same plan, same shape, one agent changed."""
        assert _validate_plan(_plan(agent, "scribe"), self.VALID) is None
        assert _validate_plan(_plan("mailer", agent), self.VALID) is None


class TestTheRealBoundary:
    """Drive run_plan_execute with a fake planner and count who actually ran.

    _validate_plan is called with valid_agents that run_plan_execute derives
    itself, so testing the validator alone assumes the very filtering it is
    meant to prove. This goes through the entry point.
    """

    @staticmethod
    def _run(monkeypatch, plan_json):
        invoked = []

        async def fake_run(agent, message, **kwargs):
            name = getattr(agent, "name", "")
            invoked.append(name)
            if name == "workflow_planner":
                return plan_json
            return f"{name} gotov"

        monkeypatch.setattr(plan_execute, "run_agent_simple", fake_run)
        monkeypatch.setenv("PLAN_EXECUTE_MIN_WORDS", "0")

        fell_back = []

        async def fallback(msg):
            fell_back.append(msg)
            return "orchestrator answer"

        answer = asyncio.run(
            plan_execute.run_plan_execute(
                "napravi ovo pa ono",
                WORKERS,
                fallback=fallback,
                session_id="s-boundary",
            )
        )
        return answer, invoked, fell_back

    def test_a_plan_of_callable_agents_runs(self, monkeypatch):
        """The control: this shape is executed, not bounced."""
        _, invoked, fell_back = self._run(monkeypatch, json.dumps(_plan("mailer", "scribe")))
        assert not fell_back
        assert "mailer" in invoked and "scribe" in invoked

    @pytest.mark.parametrize("agent", sorted(NON_CALLABLE_WORKER_AGENTS))
    def test_a_plan_naming_a_non_callable_agent_never_runs_it(self, monkeypatch, agent):
        _, invoked, fell_back = self._run(monkeypatch, json.dumps(_plan(agent, "scribe")))
        assert fell_back, "the plan should have been rejected and handed to the orchestrator"
        assert agent not in invoked
        assert "scribe" not in invoked, "no step of a rejected plan may run"
