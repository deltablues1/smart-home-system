"""Research that becomes a document must pass through the synthesizer.

The Pi pins Opus to exactly one agent -- CLAUDE_AGENT_MODELS=synthesizer=
claude-opus-5 -- so "Sonnet researches, Opus writes it up" is configured and
paid for. It only happens if a synthesizer step is actually in the chain.

The orchestrator's Rule 9b says to add one. The PLANNER, which is what runs
when USE_PLAN_EXECUTE=true (it is, on the Pi), had never heard of the
synthesizer: zero mentions in its instructions. So every multi-step research
request planned researcher -> scribe, Opus was never called, and the document
was formatted by the same model that gathered the material. Reported
2026-09-06 as "tablice su nikakve i moglo je gramatički bolje" on the Ex zones
document -- an accurate complaint about a step that never ran.

Run with:
    pytest tests/unit/test_planner_synthesizer_step.py -v
"""

from pathlib import Path

import pytest

PLANNER = (
    Path(__file__).resolve().parents[2]
    / "agents"
    / "orchestrator"
    / "planner_instructions.md"
).read_text(encoding="utf-8")


class TestThePlannerKnowsTheStep:
    def test_the_synthesizer_is_mentioned_at_all(self):
        assert "synthesizer" in PLANNER

    @pytest.mark.parametrize(
        "chain",
        [
            "`researcher` → `synthesizer` → `scribe`",
            "`researcher` → `synthesizer` → `scribe` → `mailer`",
        ],
    )
    def test_the_documented_chains_include_it(self, chain):
        assert chain in PLANNER, "the example chains are what the planner copies"

    def test_the_old_chains_are_gone(self):
        assert "- research + create doc → `researcher` → `scribe`" not in PLANNER

    def test_it_says_why_rather_than_just_what(self):
        section = PLANNER.split("### Research that becomes a document")[1]
        assert "raw material" in section
        assert "invents nothing" in section

    def test_it_carries_the_researcher_output(self):
        """A synthesizer step with no use_results would synthesize nothing."""
        section = PLANNER.split("### Research that becomes a document")[1]
        assert "use_results" in section

    def test_a_one_line_lookup_still_skips_it(self):
        """Not every saved fact is a report."""
        section = PLANNER.split("### Research that becomes a document")[1]
        assert "Skip it" in section


class TestItAgreesWithTheOrchestrator:
    """Two planners for one system; they must not disagree about the chain."""

    def test_rule_9b_still_asks_for_the_same_step(self):
        orchestrator = (
            Path(__file__).resolve().parents[2]
            / "agents"
            / "orchestrator"
            / "instructions.md"
        ).read_text(encoding="utf-8")
        rule = orchestrator.split("### Rule 9b:")[1].split("### Rule 9c")[0]
        assert "synthesizer" in rule
