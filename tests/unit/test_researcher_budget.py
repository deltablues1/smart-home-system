"""
The researcher's output budget has to hold a whole price report.

A table of five models with specs, prices, shops, dates and full source URLs
does not fit in 8192 tokens — and on Claude, adaptive thinking is drawn from
the same max_output_tokens, so 8192 (which was also exactly the thinking
floor the factory enforces) left nothing for the answer. The report came back
truncated mid-table, which reads like a bad researcher rather than a cap.

Note the budget must be set here, not in config/agent_registry.py: the registry
calls factory_func(model=config.model, **kwargs) for ADK agents and never
forwards config.config, so the values there are dead for this agent.

Run with:
    pytest tests/unit/test_researcher_budget.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.adk_agents.researcher_adk as researcher_adk  # noqa: E402
from agents.adk_agents.adk_agent_factory import (  # noqa: E402
    _CLAUDE_THINKING_MIN_OUTPUT_TOKENS,
)


def _captured_config(monkeypatch):
    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(researcher_adk, "create_adk_agent", _fake_create)
    monkeypatch.setattr(researcher_adk, "get_research_adk_tools", lambda credentials=None: [])
    researcher_adk.create_researcher_agent()
    return captured["config"]


def test_researcher_gets_room_for_a_full_report(monkeypatch):
    assert _captured_config(monkeypatch)["max_tokens"] == 16384


def test_budget_clears_the_thinking_floor(monkeypatch):
    """At the floor exactly, thinking and the answer compete for the same tokens."""
    assert _captured_config(monkeypatch)["max_tokens"] > _CLAUDE_THINKING_MIN_OUTPUT_TOKENS
