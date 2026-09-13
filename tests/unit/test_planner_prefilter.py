"""
The planner ran ahead of every request, including the ones it then declined.

USE_PLAN_EXECUTE puts a full Sonnet call (~2.7k input tokens, ~4 s) in front of
the orchestrator. Measured 2026-09-03, a voice question about heat pump prices
paid for a planner turn whose entire output was "not a multi-step chain".
"Upali svjetlo" paid for one too.

The pre-filter is deliberately generous: a false positive costs one planner
call, a false negative just means the Smart Orchestrator handles the request —
which is exactly what the planner's own "single step" verdict leads to.

Run with:
    pytest tests/unit/test_planner_prefilter.py -v
"""

import pytest

from agents.adk_agents.plan_execute import _looks_multi_step, _user_text

VOICE = (
    "[VOICE_ASSISTANT_PROFILE]\n"
    "Ti si Jarvis, glasovni kucni asistent. Tvoj stil je smiren i kratak. "
    "Govori hrvatski i o sebi govori u muskom rodu. Odgovaraj kratko i jasno.\n"
    "[/VOICE_ASSISTANT_PROFILE]\n\nKorisnik je rekao: "
)


class TestShortSingleActionsSkipThePlanner:
    @pytest.mark.parametrize(
        "message",
        [
            "upali svjetlo u dnevnoj",
            "koliko je sati",
            "ugasi bojler",
            "detaljno istraži cijene dizalica topline",  # one step, however deep
        ],
    )
    def test_skipped(self, message):
        assert _looks_multi_step(message) is False


class TestChainsStillGetAPlan:
    @pytest.mark.parametrize(
        "message",
        [
            "istraži cijene dizalica topline i pošalji mi to na mail",
            "napravi dokument pa mi ga pošalji",
            "provjeri kalendar zatim javi Ivanu",
            "research the topic and create a document",
        ],
    )
    def test_planned(self, message):
        assert _looks_multi_step(message) is True

    def test_diacritics_do_not_hide_a_joiner(self):
        """The marker list is ASCII; "pošalji" has to fold onto it."""
        assert _looks_multi_step("napravi izvještaj i pošalji ga") is True

    def test_a_line_break_does_not_hide_a_joiner(self):
        assert _looks_multi_step("napravi izvještaj\ni pošalji ga") is True

    def test_long_requests_get_a_plan_even_without_a_joiner(self):
        long = " ".join(["riječ"] * 14)
        assert _looks_multi_step(long) is True


class TestVoicePersonaIsNotCounted:
    """The persona preamble is longer than most requests; counting it would
    send every spoken word to the planner."""

    def test_persona_is_stripped(self):
        assert _user_text(VOICE + "ugasi svjetlo") == "ugasi svjetlo"

    def test_short_spoken_command_still_skips_the_planner(self):
        assert _looks_multi_step(VOICE + "ugasi svjetlo") is False

    def test_spoken_chain_still_gets_a_plan(self):
        assert _looks_multi_step(VOICE + "istraži dizalice i napravi dokument") is True


class TestEscapeHatch:
    def test_zero_disables_the_shortcut(self, monkeypatch):
        monkeypatch.setenv("PLAN_EXECUTE_MIN_WORDS", "0")
        assert _looks_multi_step("upali svjetlo") is True

    def test_garbage_threshold_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("PLAN_EXECUTE_MIN_WORDS", "nonsense")
        assert _looks_multi_step("upali svjetlo") is False
