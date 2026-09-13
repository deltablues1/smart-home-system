"""
"At most 15 tool calls" is a sentence in a prompt, not a limit.

Measured on the Pi, 2026-09-03: the researcher made 16 google_search_simple
calls in one run and was still going at 24 minutes, with the client long
disconnected. The existing loop guard did not catch it because that only
counts the *same failing* call repeated — a model inventing a new query each
time walks straight past it.

The budget is per (run, tool), not per run: an orchestrator calling sixteen
different workers is doing its job; sixteen calls to one search tool is a loop.

Run with:
    pytest tests/unit/test_tool_budget.py -v
"""

import pytest

from agents.adk_agents import adk_agent_factory as factory


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, invocation_id):
        self.invocation_id = invocation_id


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    factory._tool_call_counts.clear()
    monkeypatch.setenv("AGENT_MAX_CALLS_PER_TOOL", "3")
    yield
    factory._tool_call_counts.clear()


def _call(tool_name, run="run-1"):
    return factory._tool_budget_before(tool=_Tool(tool_name), tool_context=_Ctx(run))


class TestTheBudget:
    def test_calls_within_budget_pass(self):
        assert all(_call("google_search_simple") is None for _ in range(3))

    def test_the_call_past_the_budget_is_refused(self):
        for _ in range(3):
            _call("google_search_simple")
        result = _call("google_search_simple")
        assert "TOOL BUDGET" in result["error"]
        assert "google_search_simple" in result["error"]

    def test_the_model_is_told_to_answer_with_what_it_has(self):
        for _ in range(4):
            result = _call("google_search_simple")
        assert "Stop calling it" in result["error"]
        assert "could not confirm" in result["error"]


class TestScoping:
    def test_a_different_tool_has_its_own_budget(self):
        for _ in range(4):
            _call("google_search_simple")
        assert _call("scrape_url_advanced") is None

    def test_an_orchestrator_calling_many_workers_is_unaffected(self):
        workers = ["researcher", "scribe", "mailer", "secretary", "tracker", "analyst"]
        assert all(_call(w) is None for w in workers)

    def test_a_new_run_starts_fresh(self):
        for _ in range(4):
            _call("google_search_simple", run="run-1")
        assert _call("google_search_simple", run="run-2") is None


class TestConfig:
    def test_default_budget(self, monkeypatch):
        monkeypatch.delenv("AGENT_MAX_CALLS_PER_TOOL", raising=False)
        assert factory._tool_budget() == 12

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CALLS_PER_TOOL", "nonsense")
        assert factory._tool_budget() == 12

    def test_zero_is_floored_to_one(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CALLS_PER_TOOL", "0")
        assert factory._tool_budget() == 1
