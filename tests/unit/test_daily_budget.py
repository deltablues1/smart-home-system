"""
A ceiling on the day's model spend, enforced where the money is spent.

On 2026-08-30 false wake-word triggers drained the Anthropic credit overnight.
Token accounting recorded every one of those calls faithfully and then did
nothing with the number. A console limit is the backstop; it arrives as a silent
cutoff with no explanation of what caused it.

The ledger is persisted on purpose: the failure mode that matters is a loop that
also restarts the process, and an in-memory total would hand it a fresh
allowance each time.

Run with:
    pytest tests/unit/test_daily_budget.py -v
"""

import json

import pytest

from services import budget


@pytest.fixture(autouse=True)
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setenv("DAILY_LLM_BUDGET_USD", "1.00")
    budget.reset()
    yield tmp_path / "budget.json"
    budget.reset()


class TestAccumulating:
    def test_the_day_starts_at_zero(self):
        assert budget.spent_today() == 0.0
        assert budget.over_budget() is False

    def test_costs_add_up(self):
        budget.record(0.30)
        budget.record(0.20)
        assert budget.spent_today() == pytest.approx(0.50)

    def test_the_ceiling_trips_once_reached(self):
        budget.record(1.00)
        assert budget.over_budget() is True

    def test_remaining_counts_down(self):
        budget.record(0.75)
        assert budget.remaining() == pytest.approx(0.25)

    def test_nonsense_costs_are_ignored(self):
        budget.record(0)
        budget.record(-5)
        assert budget.spent_today() == 0.0


class TestSurvivingARestart:
    def test_the_total_is_not_reset_by_a_restart(self):
        """The loop that spends the money often restarts the process too."""
        budget.record(1.20)
        budget.reset()  # as if the service restarted
        assert budget.over_budget() is True

    def test_the_ledger_is_readable_json(self, ledger):
        budget.record(0.40)
        data = json.loads(ledger.read_text(encoding="utf-8"))
        assert data["spent"] == pytest.approx(0.40)

    def test_a_corrupt_ledger_starts_the_day_at_zero(self, ledger):
        ledger.write_text("{not json", encoding="utf-8")
        budget.reset()
        assert budget.spent_today() == 0.0


class TestDisabled:
    def test_no_ceiling_configured_never_trips(self, monkeypatch):
        monkeypatch.setenv("DAILY_LLM_BUDGET_USD", "0")
        budget.record(999)
        assert budget.over_budget() is False
        assert budget.remaining() is None

    def test_spend_is_still_recorded_without_a_ceiling(self, monkeypatch):
        monkeypatch.setenv("DAILY_LLM_BUDGET_USD", "0")
        budget.record(2.50)
        assert budget.spent_today() == pytest.approx(2.50)

    def test_garbage_ceiling_disables_rather_than_crashes(self, monkeypatch):
        monkeypatch.setenv("DAILY_LLM_BUDGET_USD", "nonsense")
        assert budget.daily_budget_usd() == 0.0


class TestTheBreaker:
    def _call(self):
        from agents.adk_agents.adk_agent_factory import _budget_before_model

        return _budget_before_model(None, None)

    def test_under_budget_lets_the_call_through(self):
        budget.record(0.10)
        assert self._call() is None

    def test_over_budget_stops_the_call(self):
        from agents.adk_agents.adk_agent_factory import DailyBudgetExceeded

        budget.record(1.50)
        with pytest.raises(DailyBudgetExceeded) as excinfo:
            self._call()
        assert "1.50" in str(excinfo.value)

    def test_the_message_says_how_to_lift_it(self):
        from agents.adk_agents.adk_agent_factory import DailyBudgetExceeded

        budget.record(1.50)
        with pytest.raises(DailyBudgetExceeded) as excinfo:
            self._call()
        assert "DAILY_LLM_BUDGET_USD" in str(excinfo.value)


class TestCostAgreesWithTheReport:
    def test_the_ledger_uses_the_same_arithmetic_as_tokens(self):
        """A ceiling computed differently from the report it is compared
        against is worse than no ceiling."""
        from tools.observability.token_stats import _Agg, cost_of

        agg = _Agg()
        agg.model = "claude-sonnet-5"
        agg.prompt, agg.cached, agg.output = 10_000, 8_000, 500

        assert cost_of("claude-sonnet-5", 10_000, 500, 8_000) == pytest.approx(agg.cost())

    def test_an_unknown_model_has_no_cost_rather_than_a_wrong_one(self):
        from tools.observability.token_stats import cost_of

        assert cost_of("some-unlisted-model", 1000, 100) is None
