"""
Home Assistant Assist ("ha-assist") is a voice channel and must reach the same
lanes as the Pi's wake word: smart-home fast path, local time/weather, and the
cheap voice_qa agent — never the 16-tool orchestrator, and never the sticky
CLASSROOM mode that a philosophy question used to switch on for every channel.

Pure routing logic — no Firestore, no LLM.

Run with:
    pytest tests/unit/test_ha_assist_routing.py -v
"""

import pytest

from interfaces import base_interface
from interfaces.base_interface import (
    BaseInterface,
    SPOKEN_CHANNEL_USER_PREFIXES,
    VOICE_ROUTING_USER_PREFIXES,
)


class _StubSystem:
    philosophy_keywords = {"filozofij", "sokrat"}


class _VoiceInterface(BaseInterface):
    """Minimal concrete BaseInterface for routing tests."""

    def __init__(self):
        super().__init__(session_prefix="test")
        self.system = _StubSystem()

    async def start(self) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    def format_response(self, response: str) -> str:  # pragma: no cover - unused
        return response


@pytest.fixture
def iface():
    return _VoiceInterface()


@pytest.fixture
def direct_routing_on(monkeypatch):
    import config.deployment_config as deployment_config

    monkeypatch.setattr(deployment_config, "is_voice_direct_routing", lambda: True)
    return deployment_config


class TestHaAssistIsAVoiceChannel:

    def test_ha_assist_gets_direct_routing(self, iface, direct_routing_on):
        assert iface._should_use_voice_direct_routing("ha-assist") is True
        # The conversation entity may append a session suffix.
        assert iface._should_use_voice_direct_routing("ha-assist-42") is True

    def test_wake_word_channel_unchanged(self, iface, direct_routing_on):
        assert iface._should_use_voice_direct_routing("rpi-voice-1") is True

    def test_text_channels_still_use_the_orchestrator(self, iface, direct_routing_on):
        assert iface._should_use_voice_direct_routing("telegram-123456789") is False
        assert iface._should_use_voice_direct_routing("web-user") is False

    def test_profile_flag_still_wins(self, iface, monkeypatch):
        import config.deployment_config as deployment_config

        monkeypatch.setattr(deployment_config, "is_voice_direct_routing", lambda: False)
        assert iface._should_use_voice_direct_routing("ha-assist") is False

    def test_ha_assist_still_counts_as_spoken_for_error_wording(self):
        # A raw litellm traceback must never be read out loud again.
        assert "ha-assist".startswith(SPOKEN_CHANNEL_USER_PREFIXES)
        assert "ha-assist" in VOICE_ROUTING_USER_PREFIXES


class TestPhoneQuestionsTakeTheCheapLane:

    def test_philosophy_question_routes_to_socrates_without_classroom(self, iface):
        # Same target as before, but as a direct worker run: `active_mode` is
        # left alone, so the next "upali svjetlo" is not answered by Socrates.
        assert iface._classify_voice_route(
            "što mi možeš reći o Sokratu"
        ) == ("agent", "socrates")
        assert getattr(iface.system, "active_mode", "LEGACY") == "LEGACY"

    def test_smart_home_command_reaches_the_fast_lane(self, iface):
        assert iface._classify_voice_route(
            "upali svjetlo u dnevnoj sobi"
        ) == ("agent", "smart_home")

    def test_general_question_defaults_to_voice_qa(self, iface):
        assert iface._classify_voice_route(
            "koliko je visok Mount Everest"
        ) == ("agent", "voice_qa")
