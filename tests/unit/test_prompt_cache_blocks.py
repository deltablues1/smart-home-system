"""
The prompt cache was being thrown away once a minute.

Anthropic caching is a prefix match, and runtime_patches marks the whole system
prompt as one cache block. Ten prompts opened with {current_datetime}, which
renders "…at 09:45 PM CET" — so on the 46th minute the entire block, including
the orchestrator's 16 KB of instructions and 16 worker descriptions, was a miss.
Measured 2026-09-03: ~15k uncached input tokens for a 464-token answer.

Nine of the ten only ever computed dates, so they lost the clock. Secretary
kept it, behind a CACHE_BREAK marker: everything before the marker is cached,
everything after is sent fresh.

Run with:
    pytest tests/unit/test_prompt_cache_blocks.py -v
"""

from pathlib import Path

import pytest

from agents.adk_agents.adk_agent_factory import _append_untrusted_content_rule
from agents.adk_agents.datetime_context import inject_datetime_context
from config.runtime_patches import CACHE_BREAK, cache_blocks_for

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

# Prompts that render a date but never need the clock: their own rules compute
# relative dates from {current_date}.
DAY_GRANULAR_AGENTS = [
    "librarian", "mailer", "smart_home", "tracker",
]

# Prompts that do need the clock. They keep it behind CACHE_BREAK so the static
# body stays a stable cached prefix: secretary schedules "za dvije minute", and
# the orchestrator answers "koliko je sati" on text channels, where the voice
# fast path does not apply.
CLOCK_BEHIND_BREAK_AGENTS = ["secretary", "orchestrator"]

ALL_AGENT_DIRS = sorted(p.parent.name for p in AGENTS_DIR.glob("*/instructions.md"))


def _instruction(agent: str) -> str:
    return (AGENTS_DIR / agent / "instructions.md").read_text(encoding="utf-8")


class TestNoPromptChangesEveryMinute:
    @pytest.mark.parametrize("agent", DAY_GRANULAR_AGENTS)
    def test_clock_is_gone(self, agent):
        assert "{current_datetime}" not in _instruction(agent)

    @pytest.mark.parametrize("agent", DAY_GRANULAR_AGENTS)
    def test_date_survived(self, agent):
        assert "{current_date}" in _instruction(agent)

    @pytest.mark.parametrize("agent", DAY_GRANULAR_AGENTS)
    def test_rendered_prompt_is_byte_identical_across_a_minute(self, agent):
        first = inject_datetime_context(_instruction(agent), "Europe/Zagreb")
        second = inject_datetime_context(_instruction(agent), "Europe/Zagreb")
        assert first == second


class TestCacheBlockSplitting:
    def test_prompt_without_marker_is_one_cached_block(self):
        blocks = cache_blocks_for("a stable prompt")
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_marker_puts_the_volatile_tail_outside_the_cache(self):
        blocks = cache_blocks_for(f"stable body\n\n{CACHE_BREAK}\n\n21:45")
        assert len(blocks) == 2
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in blocks[1]
        assert "21:45" in blocks[1]["text"]
        assert "21:45" not in blocks[0]["text"]

    def test_marker_with_nothing_after_it_stays_one_block(self):
        assert len(cache_blocks_for(f"body\n{CACHE_BREAK}\n  \n")) == 1

    def test_marker_at_the_very_start_is_not_an_empty_cached_prefix(self):
        blocks = cache_blocks_for(f"{CACHE_BREAK}\nonly volatile")
        assert len(blocks) == 1


class TestTheClockIsOnlyEverBehindTheBreak:
    """The rule, rather than a list: a minute-granular line anywhere inside the
    cached prefix invalidates the whole prompt once a minute."""

    @pytest.mark.parametrize("agent", ALL_AGENT_DIRS)
    def test_no_prompt_has_the_clock_in_its_cached_prefix(self, agent):
        static, _, _volatile = _instruction(agent).partition(CACHE_BREAK)
        assert "{current_datetime}" not in static, (
            f"{agent} would invalidate its cached prefix every minute"
        )


class TestClockBehindBreak:
    @pytest.mark.parametrize("agent", CLOCK_BEHIND_BREAK_AGENTS)
    def test_uses_the_marker(self, agent):
        assert CACHE_BREAK in _instruction(agent)

    @pytest.mark.parametrize("agent", CLOCK_BEHIND_BREAK_AGENTS)
    def test_clock_is_after_the_marker(self, agent):
        static, _, volatile = _instruction(agent).partition(CACHE_BREAK)
        assert "{current_datetime}" in volatile
        assert "{current_datetime}" not in static

    @pytest.mark.parametrize("agent", CLOCK_BEHIND_BREAK_AGENTS)
    def test_cached_prefix_is_stable_while_the_clock_moves(self, agent):
        prompt = _instruction(agent)
        a = cache_blocks_for(inject_datetime_context(prompt, "Europe/Zagreb"))
        b = cache_blocks_for(
            inject_datetime_context(prompt, "Europe/Zagreb").replace("09:45", "09:46")
        )
        assert a[0]["text"] == b[0]["text"], "the cached half must not move with the clock"

    @pytest.mark.parametrize("agent", CLOCK_BEHIND_BREAK_AGENTS)
    def test_static_untrusted_fragment_stays_on_the_cached_side(self, agent):
        """Appending it blindly would park a static block next to the clock."""
        name = "smart_orchestrator" if agent == "orchestrator" else agent
        built = _append_untrusted_content_rule(name, _instruction(agent))
        static, sep, volatile = built.partition(CACHE_BREAK)
        assert sep, "marker must survive the append"
        assert "untrusted" in static.lower()
        assert "untrusted" not in volatile.lower()
