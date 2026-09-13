"""Every agent that can trip the approval gate must know how the gate behaves.

The gate in services/approval_gate.py holds a call and returns a question. Two
prompts told the model to ask the user *before* issuing such a call instead,
which registers nothing: the user's "da" arrives with no pending action to
authorise and the write is still a full turn away. Measured 2026-09-04, a
calendar deletion took three turns for exactly that reason.

The other half is the inverse mistake. Tools the gate does NOT cover -
drive_delete_file, tasks_delete_task, sheets_clear_values - have no protection
at all, so their prompts must keep asking first. These tests pin both halves,
and tie the prompt set to the code's RULES table so a newly gated tool cannot
quietly land in a lane whose prompt never heard of the gate.

Run with:
    pytest tests/unit/test_confirmation_gate_prompt.py -v
"""

from pathlib import Path

import pytest

from agents.adk_agents.adk_agent_factory import (
    _CONFIRMATION_GATE_AGENTS,
    _append_confirmation_gate_rule,
    load_instruction_file,
    load_shared_fragment,
)
from config.runtime_patches import CACHE_BREAK
from services.approval_gate import RULES

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

# Lanes that carry the protocol in their own words instead of the shared
# fragment, because it is written against their own tool names.
SELF_DOCUMENTED_LANES = {"smart_home"}

# Prompt file backing each factory name that differs from it.
PROMPT_DIR = {"smart_orchestrator": "orchestrator"}


def _instruction(name: str) -> str:
    return load_instruction_file(PROMPT_DIR.get(name, name))


def _positions(text: str, needle: str):
    """Every index where `needle` occurs."""
    start = text.find(needle)
    while start != -1:
        yield start
        start = text.find(needle, start + 1)


class TestSharedFragment:
    def test_it_exists(self):
        assert load_shared_fragment("confirmation_gate")

    @pytest.mark.parametrize("name", sorted(_CONFIRMATION_GATE_AGENTS))
    def test_every_listed_agent_receives_it(self, name):
        built = _append_confirmation_gate_rule(name, _instruction(name))
        assert "Potvrda radnji" in built

    @pytest.mark.parametrize("name", sorted(_CONFIRMATION_GATE_AGENTS))
    def test_it_stays_on_the_cached_side_of_the_break(self, name):
        built = _append_confirmation_gate_rule(name, _instruction(name))
        static, _, volatile = built.partition(CACHE_BREAK)
        assert "Potvrda radnji" in static
        assert "Potvrda radnji" not in volatile

    @pytest.mark.parametrize("name", sorted(_CONFIRMATION_GATE_AGENTS))
    def test_appending_twice_changes_nothing(self, name):
        once = _append_confirmation_gate_rule(name, _instruction(name))
        assert _append_confirmation_gate_rule(name, once) == once

    def test_it_forbids_asking_before_the_call(self):
        fragment = load_shared_fragment("confirmation_gate")
        assert "NE pitaj prije poziva" in fragment
        assert "identičnim" in fragment  # line-wrapped from "argumentima"
        assert "samo zadržanu radnju" in fragment


class TestEveryGatedLaneKnowsTheProtocol:
    """Adding a rule to approval_gate.RULES must not skip the prompt."""

    @pytest.mark.parametrize("lane", sorted({r.lane for r in RULES.values() if r.lane}))
    def test_lane_has_the_protocol(self, lane):
        if lane in SELF_DOCUMENTED_LANES:
            text = _instruction(lane)
            assert "needs_confirmation" in text, (
                f"{lane} documents the gate itself and must keep doing so"
            )
            return
        assert lane in _CONFIRMATION_GATE_AGENTS, (
            f"approval_gate holds a tool in the '{lane}' lane, but that agent's "
            "prompt never receives the confirmation protocol"
        )

    @pytest.mark.parametrize(
        "tool,lane",
        sorted((name, rule.lane) for name, rule in RULES.items() if rule.lane),
    )
    def test_the_prompt_names_the_gated_tool(self, tool, lane):
        """The protocol says "your rules list which tools are held".

        Knowing the protocol is not the same as knowing which of your own
        tools it applies to. A tool added to an already-covered lane would
        otherwise leave the list silently stale.
        """
        assert tool in _instruction(lane), (
            f"approval_gate holds {tool} in the '{lane}' lane, but that "
            "agent's prompt never names it as held"
        )


UNGATED_DESTRUCTIVE = [
    ("librarian", "drive_delete_file"),
    ("librarian", "drive_move_file"),
    ("tracker", "tasks_delete_task"),
    ("analyst", "sheets_clear_values"),
    ("analyst", "sheets_update_values"),
    ("secretary", "calendar_update_event"),
]

# Words that make a mention an instruction rather than a table row.
CONFIRMATION_WORDS = (
    "confirm", "confirmation", "explicit yes", "ask", "potvrd", "pitaj",
)


class TestUngatedDestructiveToolsStillAskFirst:
    """The inverse mistake: dropping the only protection a tool has."""

    @pytest.mark.parametrize("agent,tool", UNGATED_DESTRUCTIVE)
    def test_the_premise_holds(self, agent, tool):
        """If one of these ever gets a gate, it belongs in the other test."""
        assert tool not in RULES, (
            f"{tool} is gated now -- move it to the gated-tool test and give "
            f"{agent}'s prompt the held-tool treatment instead"
        )

    @pytest.mark.parametrize("agent,tool", UNGATED_DESTRUCTIVE)
    def test_the_prompt_asks_before_using_it(self, agent, tool):
        """Naming it in the tool table is not protection.

        The first version asserted only that the string appeared somewhere,
        which the tool inventory at the top of every prompt satisfies on its
        own. The mention has to sit inside text that tells the model to ask.
        """
        text = _instruction(agent)
        assert tool in text, f"{agent}'s prompt never mentions {tool}"

        window = 700
        for hit in _positions(text, tool):
            around = text[max(0, hit - window):hit + window].lower()
            if any(word in around for word in CONFIRMATION_WORDS):
                return
        pytest.fail(
            f"{agent} mentions {tool} but never near an instruction to "
            "confirm; a tool-table row is not a safeguard"
        )


class TestTheSharedTextDoesNotFightLocalRules:
    """The fragment is appended AFTER each agent's own rules.

    analyst deliberately exempts two writes from confirmation - a spreadsheet it
    just created, and a cell the user dictated. A blanket "confirm everything
    that overwrites" underneath that gives the model two rules and reintroduces
    the friction the exemption exists to remove.
    """

    def test_the_fragment_defers_to_local_rules(self):
        fragment = load_shared_fragment("confirmation_gate")
        assert "imaju prednost pred ovim odlomkom" in fragment

    def test_it_does_not_open_the_door_to_invented_exceptions(self):
        fragment = load_shared_fragment("confirmation_gate")
        assert "nemoj izmišljati nove" in fragment

    def test_the_analyst_exception_is_marked_exhaustive(self):
        text = _instruction("analyst")
        assert "they are exhaustive" in text
        assert "do not ask again on top of it" in text


class TestRefusalIsScopedToTheHeldAction:
    """"Nothing was changed" is false when three earlier steps succeeded."""

    def test_the_fragment_scopes_it(self):
        fragment = load_shared_fragment("confirmation_gate")
        assert "zadržana radnja** nije izvršena" in fragment

    def test_it_names_the_wrong_claim(self):
        fragment = load_shared_fragment("confirmation_gate")
        assert "je netočno kad je mail" in fragment
