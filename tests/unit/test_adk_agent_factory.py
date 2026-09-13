"""What the model behind an agent will actually accept.

Claude Sonnet 5 and the Opus 4.7+ line reject `temperature` outright (HTTP 400)
and spend part of max_output_tokens on thinking. create_adk_agent knows this;
agents that set generate_content_config by hand went around it and broke every
request through the orchestrator, so the rule now lives in one shared helper.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.adk_agents.adk_agent_factory import (  # noqa: E402
    claude_safe_generation_kwargs,
)


def test_a_model_that_rejects_sampling_gets_no_temperature(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CLAUDE_GEMINI_ONLY_AGENTS", "")
    monkeypatch.setenv("CLAUDE_FLASH_MODEL", "claude-sonnet-5")

    kwargs = claude_safe_generation_kwargs(
        "workflow_planner", "gemini-3.5-flash",
        temperature=0.0, max_output_tokens=2048,
    )

    assert "temperature" not in kwargs


def test_thinking_models_get_output_headroom(monkeypatch):
    """2048 tokens shared with thinking is how a plan gets cut off mid-JSON."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CLAUDE_GEMINI_ONLY_AGENTS", "")
    monkeypatch.setenv("CLAUDE_FLASH_MODEL", "claude-sonnet-5")

    kwargs = claude_safe_generation_kwargs(
        "workflow_planner", "gemini-3.5-flash",
        temperature=0.0, max_output_tokens=2048,
    )

    assert kwargs["max_output_tokens"] >= 8192


def test_a_model_that_takes_sampling_keeps_it(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CLAUDE_GEMINI_ONLY_AGENTS", "")
    monkeypatch.setenv("CLAUDE_FLASH_MODEL", "claude-sonnet-4-6")

    kwargs = claude_safe_generation_kwargs(
        "workflow_summarizer", "gemini-3.5-flash",
        temperature=0.4, max_output_tokens=4096,
    )

    assert kwargs == {"temperature": 0.4, "max_output_tokens": 4096}


def test_gemini_deployments_are_left_alone(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    kwargs = claude_safe_generation_kwargs(
        "smart_orchestrator", "gemini-3.5-flash",
        temperature=0.2, max_output_tokens=8192,
    )

    assert kwargs == {"temperature": 0.2, "max_output_tokens": 8192}
