"""The RAG corpus is a repository, not a certificate of authenticity.

Two problems met in this one agent. It is built directly with LlmAgent rather
than through create_adk_agent, so it never received the shared prompt-injection
boundary every other content-reading agent gets - while reading a corpus whose
documents can contain text shaped like instructions.

And its prompt said that content mentioning a particular title "TO JE STVARAN,
VALJAN IZVOR". The rule it was reaching for is real and worth keeping: a
document newer than the model's training is not fictional just because the
model does not recognise it. But the inverse does not follow. Appearing in a
store somebody writes into says the text is there, not who wrote it.

Run with:
    pytest tests/unit/test_christian_guide_prompt.py -v
"""

from agents.adk_agents.adk_agent_factory import (
    _UNTRUSTED_CONTENT_AGENTS,
    _append_untrusted_content_rule,
    load_instruction_file,
)

PROMPT = load_instruction_file("christian_guide")


class TestUntrustedContentBoundary:
    def test_the_agent_is_registered(self):
        assert "christian_guide" in _UNTRUSTED_CONTENT_AGENTS

    def test_the_boundary_is_appended(self):
        built = _append_untrusted_content_rule("christian_guide", PROMPT)
        assert "NEPOUZDANI PODATAK" in built

    def test_the_builder_applies_it(self):
        """It is built outside the factory, so the call must be in its module."""
        import inspect

        from agents.adk_agents import christian_guide_adk

        src = inspect.getsource(christian_guide_adk)
        assert "_append_untrusted_content_rule" in src


class TestProvenanceInsteadOfBlanketValidity:
    def test_the_blanket_claim_is_gone(self):
        assert "STVARAN, VALJAN IZVOR" not in PROMPT

    def test_it_still_refuses_to_call_an_unfamiliar_document_fictional(self):
        assert "NIJE dokaz da ne postoji" in PROMPT

    def test_it_also_refuses_to_call_it_authentic(self):
        assert "nije ni dokaz da je dokument autentican" in PROMPT

    def test_it_asks_for_the_record_s_own_provenance(self):
        section = PROMPT.split("## Dokument koji ne prepoznajes")[1]
        for field in ["naslov", "autora", "datum"]:
            assert field in section

    def test_the_pope_stays_a_fact(self):
        assert "Lav XIV." in PROMPT
