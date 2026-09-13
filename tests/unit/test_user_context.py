"""
Unit tests for the request-scoped UserContext and the shared
untrusted-content prompt fragment appended to content-consuming agents.

Pure logic — no LLM, no network.

Run with:
    pytest tests/unit/test_user_context.py -v
"""

import pytest

from agents.adk_agents.adk_agent_factory import (
    _UNTRUSTED_CONTENT_AGENTS,
    _append_untrusted_content_rule,
    load_shared_fragment,
)
from config.user_context import UserContext, get_default_user_context


class TestUserContext:
    def test_defaults_single_user(self):
        ctx = get_default_user_context()
        assert ctx.user_id == "tomislav"
        assert ctx.timezone == "Europe/Zagreb"
        assert ctx.language == "hr"
        assert ctx.channel == "web"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_USER_ID", "ana")
        monkeypatch.setenv("DEFAULT_USER_TIMEZONE", "Europe/Berlin")
        ctx = get_default_user_context(channel="voice")
        assert ctx.user_id == "ana"
        assert ctx.timezone == "Europe/Berlin"
        assert ctx.channel == "voice"

    def test_frozen(self):
        ctx = get_default_user_context()
        with pytest.raises(Exception):
            ctx.user_id = "other"

    def test_with_channel_returns_new_instance(self):
        ctx = get_default_user_context()
        voice_ctx = ctx.with_channel("voice")
        assert voice_ctx.channel == "voice"
        assert ctx.channel == "web"
        assert voice_ctx.user_id == ctx.user_id


class TestUntrustedContentFragment:
    def test_fragment_loads(self):
        fragment = load_shared_fragment("untrusted_content")
        assert fragment is not None
        assert "NEPOUZDANI PODATAK" in fragment
        assert "untrusted" in fragment.lower()

    def test_missing_fragment_returns_none(self):
        assert load_shared_fragment("does_not_exist_xyz") is None

    @pytest.mark.parametrize("agent", sorted(_UNTRUSTED_CONTENT_AGENTS))
    def test_appended_for_content_agents(self, agent):
        result = _append_untrusted_content_rule(agent, "Base instructions.")
        assert "NEPOUZDANI PODATAK" in result
        assert result.startswith("Base instructions.")

    def test_real_orchestrator_name_is_protected(self):
        # The coordinator agent is created as "smart_orchestrator" — a stale
        # "orchestrator" entry once left the REAL orchestrator unprotected.
        assert "smart_orchestrator" in _UNTRUSTED_CONTENT_AGENTS
        assert "orchestrator" not in _UNTRUSTED_CONTENT_AGENTS

    @pytest.mark.parametrize("agent", ["smart_home", "voice_qa", "socrates"])
    def test_not_appended_for_other_agents(self, agent):
        result = _append_untrusted_content_rule(agent, "Base instructions.")
        assert result == "Base instructions."

    def test_idempotent(self):
        once = _append_untrusted_content_rule("mailer", "Base instructions.")
        twice = _append_untrusted_content_rule("mailer", once)
        assert twice == once
