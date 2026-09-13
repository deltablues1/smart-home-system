"""Two text parts must not be glued into one word.

A model that speaks, calls a tool, then speaks again emits two text parts.
run_agent_simple concatenated them raw, so a real answer came back as
"...Provjeravam Poslano/Primljeno za trag.Provjerio sam -- mail je stvarno
stigao" (observed 2026-09-06 during the unknown-outcome probe). On the voice
lane the same join also removes the pause, because the sentence boundary is
gone before TTS ever sees it.

Run with:
    pytest tests/unit/test_runner_text_joining.py -v
"""

import pytest

from agents.adk_agents.runner_utils import _append_text


class TestSentenceBoundariesSurvive:
    @pytest.mark.parametrize("ender", [".", "!", "?", ":"])
    def test_a_finished_sentence_gets_a_paragraph_break(self, ender):
        joined = _append_text(f"Provjeravam trag{ender}", "Provjerio sam.")
        assert joined == f"Provjeravam trag{ender}\n\nProvjerio sam."

    def test_the_observed_failure_no_longer_reproduces(self):
        joined = _append_text(
            "Neću ponoviti slanje dok ne provjerim. Provjeravam Poslano za trag.",
            "Provjerio sam — mail je stvarno stigao.",
        )
        assert "trag.Provjerio" not in joined


class TestNothingElseIsDisturbed:
    def test_an_unfinished_clause_gets_only_a_space(self):
        assert _append_text("bez tocke", "nastavak") == "bez tocke nastavak"

    def test_existing_whitespace_is_left_alone(self):
        assert _append_text("kraj ", "nastavak") == "kraj nastavak"
        assert _append_text("kraj", "\nnastavak") == "kraj\nnastavak"

    def test_the_first_part_passes_through_untouched(self):
        assert _append_text("", "samo jedno") == "samo jedno"

    def test_a_single_part_answer_is_byte_identical(self):
        """The overwhelmingly common case must not gain stray whitespace."""
        only = "Poslao sam email Tomislavu."
        assert _append_text("", only) == only
