"""Tests for the Wyoming bridge that gives Home Assistant Jarvis's voice."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("wyoming", reason="wyoming protocol library not installed")

from wyoming.asr import Transcript  # noqa: E402
from wyoming.audio import AudioChunk, AudioStart, AudioStop  # noqa: E402
from wyoming.info import Describe  # noqa: E402
from wyoming.tts import Synthesize  # noqa: E402

from services.wyoming_bridge import JarvisEventHandler, build_info  # noqa: E402

SETTINGS = {
    "tts_url": "http://127.0.0.1:8000/api/tts",
    "api_token": "token",
    "tts_timeout": 60,
}


def _handler():
    """A handler with its socket replaced by a recorder."""
    handler = JarvisEventHandler.__new__(JarvisEventHandler)
    handler._info = build_info(["hr", "en"], "cedar")
    handler._settings = dict(SETTINGS)
    handler._audio = bytearray()
    handler._rate = 16000
    handler._language = None
    handler.written = []
    handler.write_event = AsyncMock(side_effect=lambda ev: handler.written.append(ev))
    return handler


def test_describe_advertises_both_services():
    info = build_info(["hr", "en"], "cedar")

    assert [p.name for p in info.asr] == ["jarvis-stt"]
    assert [p.name for p in info.tts] == ["jarvis-tts"]
    assert [v.name for p in info.tts for v in p.voices] == ["cedar"]
    assert "hr" in [lang for p in info.asr for m in p.models for lang in m.languages]


def test_describe_is_answered_with_info():
    handler = _handler()

    asyncio.run(handler.handle_event(Describe().event()))

    assert handler.written and handler.written[0].type == "info"


def test_audio_is_collected_and_transcribed():
    handler = _handler()
    speech = b"\x01\x02" * 16000  # a second of 16-bit audio

    async def scenario():
        await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
        await handler.handle_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=speech).event()
        )
        await handler.handle_event(AudioStop().event())

    stt = MagicMock()
    stt.return_value.transcribe_pcm16 = AsyncMock(return_value="upali svjetlo")
    with patch("services.audio.stt_service.STTService", stt):
        asyncio.run(scenario())

    assert Transcript.from_event(handler.written[-1]).text == "upali svjetlo"
    assert stt.return_value.transcribe_pcm16.await_args.kwargs["sample_rate"] == 16000


def test_a_stray_click_is_not_sent_for_transcription():
    """Chirp costs a network round trip; a fraction of a second is never speech."""
    handler = _handler()

    async def scenario():
        await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
        await handler.handle_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00" * 200).event()
        )
        await handler.handle_event(AudioStop().event())

    stt = MagicMock()
    with patch("services.audio.stt_service.STTService", stt):
        asyncio.run(scenario())

    stt.assert_not_called()
    assert Transcript.from_event(handler.written[-1]).text == ""


def test_a_failed_transcription_returns_empty_not_a_dropped_socket():
    """Assist should say it did not understand, not lose the connection."""
    handler = _handler()

    async def scenario():
        await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
        await handler.handle_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x02" * 16000).event()
        )
        await handler.handle_event(AudioStop().event())

    stt = MagicMock()
    stt.return_value.transcribe_pcm16 = AsyncMock(side_effect=RuntimeError("Chirp down"))
    with patch("services.audio.stt_service.STTService", stt):
        asyncio.run(scenario())

    assert Transcript.from_event(handler.written[-1]).text == ""


def test_synthesis_streams_audio_with_the_declared_rate():
    handler = _handler()
    pcm = b"\x05\x06" * 20000

    with patch.object(JarvisEventHandler, "_fetch_tts", return_value=pcm):
        asyncio.run(handler.handle_event(Synthesize(text="Bok").event()))

    start = next(e for e in handler.written if AudioStart.is_type(e.type))
    assert AudioStart.from_event(start).rate == 24000
    streamed = b"".join(
        AudioChunk.from_event(e).audio for e in handler.written if AudioChunk.is_type(e.type)
    )
    assert streamed == pcm
    assert AudioStop.is_type(handler.written[-1].type)


def test_synthesis_failure_still_closes_the_stream():
    """Without a start/stop pair Assist waits for audio that never comes."""
    handler = _handler()

    with patch.object(JarvisEventHandler, "_fetch_tts", side_effect=RuntimeError("TTS 500")):
        asyncio.run(handler.handle_event(Synthesize(text="Bok").event()))

    assert AudioStart.is_type(handler.written[0].type)
    assert AudioStop.is_type(handler.written[-1].type)


def test_empty_text_is_not_sent_to_the_tts_service():
    handler = _handler()

    with patch.object(JarvisEventHandler, "_fetch_tts") as fetch:
        asyncio.run(handler.handle_event(Synthesize(text="   ").event()))

    fetch.assert_not_called()
    assert AudioStop.is_type(handler.written[-1].type)
