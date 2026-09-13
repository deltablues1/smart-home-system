"""
Prompts must know the date, and must not change every minute because of it.

Two separate failures met in one place. Agents that read the web (researcher,
scraper, synthesizer, voice_qa) had no date at all, so rules about freshness and
"na današnji dan" hung in the air. Meanwhile ten other prompts opened with
{current_datetime}, which renders to the minute — and since the whole system
prompt is marked as one Anthropic cache block, that line invalidated the entire
cached prefix once a minute.

{current_date} renders to the day, so it satisfies the first need without
causing the second. These tests keep both properties from regressing.

Run with:
    pytest tests/unit/test_prompt_date_hygiene.py -v
"""

from datetime import datetime
from pathlib import Path

import pytest
import pytz

from agents.adk_agents.datetime_context import (
    has_datetime_placeholders,
    inject_datetime_context,
)

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

# Agents that read external content and reason about "now".
DATED_AGENTS = ["researcher", "scraper", "synthesizer", "voice_qa"]


def _instruction(agent: str) -> str:
    return (AGENTS_DIR / agent / "instructions.md").read_text(encoding="utf-8")


class TestWebFacingAgentsKnowTheDate:
    @pytest.mark.parametrize("agent", DATED_AGENTS)
    def test_has_a_date_placeholder(self, agent):
        assert has_datetime_placeholders(_instruction(agent)), (
            f"{agent} cannot judge source freshness without a date"
        )

    @pytest.mark.parametrize("agent", DATED_AGENTS)
    def test_renders_to_todays_date(self, agent):
        rendered = inject_datetime_context(_instruction(agent), "Europe/Zagreb")
        today = datetime.now(pytz.timezone("Europe/Zagreb")).strftime("%Y-%m-%d")
        assert today in rendered
        assert "{current_date}" not in rendered


class TestNoPromptRendersToTheMinute:
    """The cache invalidator: {current_datetime} carries %I:%M."""

    @pytest.mark.parametrize("agent", DATED_AGENTS)
    def test_web_facing_agents_use_day_granularity(self, agent):
        assert "{current_datetime}" not in _instruction(agent), (
            f"{agent} would invalidate its prompt cache every minute"
        )
