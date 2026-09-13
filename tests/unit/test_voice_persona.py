from config.voice_persona import (
    build_live_system_prompt,
    get_voice_assistant_name,
    wrap_agent_voice_message,
)


def test_voice_persona_defaults_to_jarvis(monkeypatch):
    monkeypatch.delenv("VOICE_ASSISTANT_NAME", raising=False)

    assert get_voice_assistant_name() == "Jarvis"


def test_live_prompt_uses_env_overrides(monkeypatch):
    monkeypatch.setenv("VOICE_ASSISTANT_NAME", "Domar")
    monkeypatch.setenv("VOICE_ASSISTANT_STYLE", "miran i odlucan")

    prompt = build_live_system_prompt()

    assert "Ti si Domar" in prompt
    assert "miran i odlucan" in prompt
    assert "u muskom rodu" in prompt


def test_wrap_agent_voice_message_embeds_transcript(monkeypatch):
    monkeypatch.setenv("VOICE_ASSISTANT_NAME", "Jarvis")

    message = wrap_agent_voice_message("otvori garazna vrata")

    assert "[VOICE_ASSISTANT_PROFILE]" in message
    assert "Ti si Jarvis" in message
    assert message.endswith("Korisnik je rekao: otvori garazna vrata")
