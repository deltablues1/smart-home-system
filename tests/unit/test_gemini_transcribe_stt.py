"""
STT_ENGINE=gemini_transcribe — the Gemini 3.5 Transcribe path.

What matters here is not that a request goes out, but *what* it says: the
language is pinned (detection is what mistook Croatian for Macedonian on
OpenAI and Czech on xAI), the audio travels inline inside a user_input turn —
the May 2026 schema, which the SDK bundled with ADK still gets wrong — and a
failure falls back to Chirp instead of leaving the house deaf.

Run with:
    pytest tests/unit/test_gemini_transcribe_stt.py -v
"""

import base64

import pytest

from services.audio import stt_service as stt


class _FakeAPI:
    """Stands in for the Interactions endpoint and remembers what it was sent."""

    def __init__(self, text="Upali svjetlo u dnevnoj sobi.", error=None, body=None):
        self.text = text
        self.error = error
        self.body = body
        self.calls = []

    def __call__(self, payload, timeout=30.0):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return self.body if self.body is not None else {"output_text": self.text}


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("STT_ENGINE", "gemini_transcribe")
    monkeypatch.setenv("STT_LANGUAGE_CODE", "hr-HR")
    for name in ("GEMINI_TRANSCRIBE_MODEL", "GEMINI_TRANSCRIBE_LANGUAGES",
                 "GEMINI_TRANSCRIBE_MODE", "GEMINI_TRANSCRIBE_VOCABULARY",
                 "STT_FALLBACK_ENGINE"):
        monkeypatch.delenv(name, raising=False)
    return stt.STTService()


def _install(monkeypatch, api):
    monkeypatch.setattr(stt, "post_interaction", api)
    return api


class TestRequestShape:

    @pytest.mark.asyncio
    async def test_audio_travels_inline_with_the_language_pinned(self, engine, monkeypatch):
        fake = _install(monkeypatch, _FakeAPI())

        text = await engine.transcribe(b"RIFFfake-wav-bytes", "audio/wav")

        assert text == "Upali svjetlo u dnevnoj sobi."
        payload = fake.calls[0]
        assert payload["model"] == "gemini-3.5-transcribe"
        turn = payload["input"][0]
        assert turn["type"] == "user_input"
        item = turn["content"][0]
        assert item["type"] == "audio"
        assert item["mime_type"] == "audio/wav"
        assert base64.b64decode(item["data"]) == b"RIFFfake-wav-bytes"
        assert "uri" not in item  # no Files API round trip
        config = payload["generation_config"]["transcription_config"]
        assert config["language_codes"] == ["hr-HR"]
        # Nothing is sent that was not asked for.
        assert "mode" not in config and "custom_vocabulary" not in config

    @pytest.mark.asyncio
    async def test_wav_aliases_and_pcm_carry_their_rate(self, engine, monkeypatch):
        fake = _install(monkeypatch, _FakeAPI())

        await engine.transcribe(b"pcm", "audio/L16")

        item = fake.calls[0]["input"][0]["content"][0]
        assert item["mime_type"] == "audio/l16"
        assert item["rate"] == 16000 and item["channels"] == 1

    @pytest.mark.asyncio
    async def test_mode_and_vocabulary_come_from_the_environment(self, engine, monkeypatch):
        monkeypatch.setenv("GEMINI_TRANSCRIBE_MODE", "smart")
        monkeypatch.setenv("GEMINI_TRANSCRIBE_VOCABULARY", "Jarvis, kupaona , bojler")
        fake = _install(monkeypatch, _FakeAPI())

        await engine.transcribe(b"wav", "audio/wav")

        config = fake.calls[0]["generation_config"]["transcription_config"]
        assert config["mode"] == {"type": "smart"}
        assert config["custom_vocabulary"] == ["Jarvis", "kupaona", "bojler"]

    @pytest.mark.asyncio
    async def test_vocabulary_is_cut_at_the_documented_limit(self, engine, monkeypatch):
        monkeypatch.setenv("GEMINI_TRANSCRIBE_VOCABULARY", ",".join(f"t{i}" for i in range(1200)))
        fake = _install(monkeypatch, _FakeAPI())

        await engine.transcribe(b"wav", "audio/wav")

        config = fake.calls[0]["generation_config"]["transcription_config"]
        assert len(config["custom_vocabulary"]) == 1000

    @pytest.mark.asyncio
    async def test_several_languages_can_be_listed(self, engine, monkeypatch):
        monkeypatch.setenv("GEMINI_TRANSCRIBE_LANGUAGES", "hr-HR, en-US")
        fake = _install(monkeypatch, _FakeAPI())

        await engine.transcribe(b"wav", "audio/wav")

        config = fake.calls[0]["generation_config"]["transcription_config"]
        assert config["language_codes"] == ["hr-HR", "en-US"]


class TestResponseReading:

    @pytest.mark.asyncio
    async def test_transcript_is_read_from_the_steps_when_the_shortcut_is_absent(
        self, engine, monkeypatch
    ):
        _install(monkeypatch, _FakeAPI(body={
            "steps": [{"type": "model_output",
                       "content": [{"type": "text", "text": "Ugasi bojler u kupaonici."}]}]
        }))

        assert await engine.transcribe(b"wav", "audio/wav") == "Ugasi bojler u kupaonici."


class TestFailureFallsBackToChirp:

    @pytest.mark.asyncio
    async def test_api_failure_is_answered_by_chirp(self, engine, monkeypatch):
        _install(monkeypatch, _FakeAPI(error=RuntimeError("Interactions API 429: credits depleted")))
        monkeypatch.setattr(
            stt.STTService, "_transcribe_cloud_sync", lambda self, audio: "chirp je čuo ovo"
        )

        assert await engine.transcribe(b"wav", "audio/wav") == "chirp je čuo ovo"

    @pytest.mark.asyncio
    async def test_unsupported_container_falls_back_rather_than_failing(self, engine, monkeypatch):
        _install(monkeypatch, _FakeAPI())
        monkeypatch.setattr(
            stt.STTService, "_transcribe_cloud_sync", lambda self, audio: "chirp je čuo webm"
        )

        assert await engine.transcribe(b"webm", "audio/webm") == "chirp je čuo webm"

    @pytest.mark.asyncio
    async def test_fallback_can_be_switched_off(self, engine, monkeypatch):
        monkeypatch.setenv("STT_FALLBACK_ENGINE", "none")
        _install(monkeypatch, _FakeAPI(error=RuntimeError("boom")))

        with pytest.raises(Exception, match="boom"):
            await engine.transcribe(b"wav", "audio/wav")


class TestOtherEnginesAreUntouched:

    @pytest.mark.asyncio
    async def test_chirp_never_reaches_the_interactions_api(self, monkeypatch):
        monkeypatch.setenv("STT_ENGINE", "chirp")
        service = stt.STTService()
        fake = _install(monkeypatch, _FakeAPI())
        monkeypatch.setattr(
            stt.STTService, "_transcribe_cloud_sync", lambda self, audio: "chirp"
        )

        assert await service.transcribe(b"wav", "audio/wav") == "chirp"
        assert fake.calls == []
