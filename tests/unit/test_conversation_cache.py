"""
Caching the system prompt is the small half of an agentic loop.

Measured on the Pi, 2026-09-03: one research turn made eight researcher calls
totalling 1,176,439 input tokens and read 43,057 from cache — $6.14 for a
single question. Each call resent the whole ReAct history, every search result
and every scraped page, and nothing was caching any of it because the patch
only ever marked the system message.

A breakpoint at the end of the conversation makes each call read what the
previous ones accumulated and write only the delta.

Run with:
    pytest tests/unit/test_conversation_cache.py -v
"""

import pytest

from config.runtime_patches import mark_conversation_prefix


def _cc(message):
    content = message["content"]
    if isinstance(content, list):
        return [b.get("cache_control") for b in content]
    return None


class TestBreakpointPlacement:
    def test_the_last_message_gets_the_breakpoint(self):
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "istraži cijene"},
            {"role": "assistant", "content": "searching"},
            {"role": "user", "content": "tool result: 40kb of scraped page"},
        ]
        assert mark_conversation_prefix(messages) is True
        assert _cc(messages[-1]) == [{"type": "ephemeral"}]

    def test_earlier_messages_are_left_alone(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        mark_conversation_prefix(messages)
        assert messages[0]["content"] == "a"
        assert messages[1]["content"] == "b"

    def test_the_system_message_is_not_the_target(self):
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        mark_conversation_prefix(messages)
        assert messages[0]["content"] == "prompt", "system caching is handled separately"


class TestWhenNotToBother:
    def test_a_single_user_turn_has_nothing_accumulated(self):
        messages = [{"role": "system", "content": "p"}, {"role": "user", "content": "bok"}]
        assert mark_conversation_prefix(messages) is False

    def test_an_empty_conversation_is_skipped(self):
        assert mark_conversation_prefix([]) is False
        assert mark_conversation_prefix(None) is False

    def test_blank_content_is_skipped(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "   "},
        ]
        assert mark_conversation_prefix(messages) is False


class TestBlockContent:
    def test_the_final_block_is_marked(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]},
        ]
        assert mark_conversation_prefix(messages) is True
        assert _cc(messages[-1]) == [None, {"type": "ephemeral"}]

    def test_unrecognised_block_shapes_are_left_alone(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": [{"no_type_field": True}]},
        ]
        assert mark_conversation_prefix(messages) is False


class TestAgentToolArgsGuard:
    """google.adk AgentTool reads args['request'] with no default, so an
    argument-less sub-agent call raises out of the runner and kills the turn."""

    def _guard(self, tool, args):
        from agents.adk_agents.adk_agent_factory import _agent_tool_args_guard

        return _agent_tool_args_guard(tool=tool, args=args)

    class _AgentTool:
        name = "scribe"
        agent = object()

    class _PlainTool:
        name = "mqtt_list_devices"

    def test_empty_args_are_corrected_not_raised(self):
        result = self._guard(self._AgentTool(), {})
        assert "MISSING ARGUMENT" in result["error"]
        assert "scribe" in result["error"]

    def test_a_blank_request_is_also_caught(self):
        assert self._guard(self._AgentTool(), {"request": "   "}) is not None

    def test_a_real_request_passes(self):
        assert self._guard(self._AgentTool(), {"request": "napravi dokument"}) is None

    def test_a_plain_tool_with_no_arguments_is_left_alone(self):
        """Plenty of tools legitimately take none — the guard is for sub-agents."""
        assert self._guard(self._PlainTool(), {}) is None

    def test_the_correction_says_the_worker_has_no_memory(self):
        result = self._guard(self._AgentTool(), None)
        assert "no memory" in result["error"]
