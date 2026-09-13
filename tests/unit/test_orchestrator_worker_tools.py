"""voice_qa runs in front of the orchestrator, not behind it.

The voice lane routes to voice_qa first; when it decides a request needs a
tool it answers with the [[ESCALATE]] sentinel, and base_interface unwraps that
- but only when voice_qa was the routed agent. Nothing unwraps it when the
orchestrator calls voice_qa as one of its own tools, so the sentinel would have
reached the user as literal text.

It was in the worker list (get_worker_agent_names excludes only orchestration
infrastructure) and advertised in the agent reference table. It stays loaded, because the voice lane looks it up in
system.worker_agents - it is just not callable.

Run with:
    pytest tests/unit/test_orchestrator_worker_tools.py -v
"""

import pytest
from google.adk.agents import LlmAgent

from agents.adk_agents.adk_agent_factory import load_instruction_file
from agents.adk_agents.smart_orchestrator import create_smart_orchestrator
from config.agent_registry import get_worker_agent_names
from interfaces.base_interface import ESCALATE_SENTINEL


def _worker(name: str) -> LlmAgent:
    return LlmAgent(
        name=name, model="gemini-3.5-flash", description=f"{name} desc", instruction="x"
    )


@pytest.fixture(scope="module")
def orchestrator():
    return create_smart_orchestrator(
        model="gemini-3.5-flash",
        worker_agents=[_worker("voice_qa"), _worker("mailer"), _worker("scribe")],
    )


class TestVoiceQaIsNotCallable:
    def test_it_is_not_wrapped_as_a_tool(self, orchestrator):
        names = {getattr(t, "name", "") for t in orchestrator.tools}
        assert "voice_qa" not in names

    def test_the_other_workers_still_are(self, orchestrator):
        names = {getattr(t, "name", "") for t in orchestrator.tools}
        assert {"mailer", "scribe"} <= names

    def test_it_is_absent_from_the_rendered_worker_list(self, orchestrator):
        # The prompt is an instruction provider, re-rendered per invocation so
        # the clock cannot freeze at boot time.
        instruction = orchestrator.instruction
        rendered = instruction(None) if callable(instruction) else instruction
        assert "**mailer**" in rendered, "sanity: the worker list is rendered here"
        assert "**voice_qa**" not in rendered

    def test_the_reference_table_no_longer_advertises_it(self):
        prompt = load_instruction_file("orchestrator")
        assert "| voice_qa |" not in prompt
        assert "deliberately absent" in prompt


class TestItStaysLoadedForTheVoiceLane:
    """Excluding it from the registry instead would break the fast path."""

    def test_the_registry_still_yields_it(self):
        assert "voice_qa" in get_worker_agent_names()

    def test_the_sentinel_is_still_the_contract(self):
        assert ESCALATE_SENTINEL in load_instruction_file("voice_qa")
