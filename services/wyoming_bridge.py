"""Wyoming bridge: gives Home Assistant Jarvis's Croatian voice.

Assist needs speech-to-text and text-to-speech to turn a spoken question into a
spoken answer, and Home Assistant's own options are weak here. The Whisper
add-on writes poor Croatian and would compete for the HA Pi's CPU, and Google
Translate TTS speaks English. Jarvis already solved both on its own Pi: Gemini
Transcribe for recognition (Cloud Chirp as the fallback), picked after OpenAI
and xAI both mis-detected Croatian as other Slavic languages, and OpenAI
'cedar' for the voice the household already knows. Wyoming is Home Assistant's
protocol for external voice services, so this serves what already exists
rather than installing weaker copies next door.

One server advertises both services, so Home Assistant needs a single entry and
the Pi a single systemd unit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import (
    AsrModel,
    AsrProgram,
    Attribution,
    Describe,
    Info,
    TtsProgram,
    TtsVoice,
)
from wyoming.server import AsyncEventHandler
from wyoming.tts import Synthesize

logger = logging.getLogger(__name__)

# Jarvis's TTS endpoint answers PCM16 at 24 kHz, which is what gets announced to
# Home Assistant so it does not resample needlessly.
TTS_SAMPLE_RATE = 24000
TTS_SAMPLE_WIDTH = 2
TTS_CHANNELS = 1

# Audio is handed over in pieces so Home Assistant can start playing before the
# whole answer has been synthesised.
_CHUNK_BYTES = 4096

_ATTRIBUTION = Attribution(
    name="Jarvis",
    url="https://github.com/deltablues1/smart-home-system",
)


def build_info(languages: list[str], voice_name: str) -> Info:
    """Describe both services in the shape Home Assistant expects."""
    return Info(
        asr=[
            AsrProgram(
                name="jarvis-stt",
                description="Jarvis speech-to-text (Google Cloud Chirp)",
                attribution=_ATTRIBUTION,
                installed=True,
                version="1.0.0",
                models=[
                    AsrModel(
                        name=os.getenv("STT_CLOUD_MODEL", "chirp_2"),
                        description="Croatian recognition as used by the wake-word loop",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        version=None,
                        languages=languages,
                    )
                ],
            )
        ],
        tts=[
            TtsProgram(
                name="jarvis-tts",
                description="Jarvis text-to-speech (OpenAI)",
                attribution=_ATTRIBUTION,
                installed=True,
                version="1.0.0",
                voices=[
                    TtsVoice(
                        name=voice_name,
                        description="The voice Jarvis already speaks with at home",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        version=None,
                        languages=languages,
                    )
                ],
            )
        ],
    )


class JarvisEventHandler(AsyncEventHandler):
    """Serves one Wyoming connection: either a transcription or a synthesis."""

    def __init__(self, wyoming_info: Info, settings: dict, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._info = wyoming_info
        self._settings = settings
        self._audio = bytearray()
        self._rate = 16000
        self._language: str | None = None

    async def handle_event(self, event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self._info.event())
            return True

        if Transcribe.is_type(event.type):
            self._language = Transcribe.from_event(event).language
            return True

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self._rate = start.rate
            self._audio = bytearray()
            return True

        if AudioChunk.is_type(event.type):
            self._audio.extend(AudioChunk.from_event(event).audio)
            return True

        if AudioStop.is_type(event.type):
            await self._finish_transcription()
            return False

        if Synthesize.is_type(event.type):
            await self._speak(Synthesize.from_event(event).text)
            return False

        return True

    # --- speech to text ---------------------------------------------------

    async def _finish_transcription(self) -> None:
        seconds = len(self._audio) / float(self._rate * 2)
        text = ""

        if len(self._audio) < self._rate:  # under half a second of 16-bit audio
            logger.info("Ignoring %.2fs of audio, too short to transcribe", seconds)
        else:
            try:
                from services.audio.stt_service import STTService

                text = await STTService().transcribe_pcm16(
                    bytes(self._audio),
                    sample_rate=self._rate,
                    language=self._language,
                )
                logger.info("Transcribed %.1fs: %s", seconds, text[:120])
            except Exception as err:  # noqa: BLE001 - report, never crash the socket
                # An empty transcript makes Assist say it did not understand,
                # which is the truth and better than dropping the connection.
                logger.error("Transcription failed after %.1fs: %s", seconds, err)

        await self.write_event(Transcript(text=text).event())

    # --- text to speech ---------------------------------------------------

    async def _speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            await self._write_silence()
            return

        try:
            audio = await asyncio.to_thread(self._fetch_tts, text)
        except Exception as err:  # noqa: BLE001
            logger.error("Synthesis failed: %s", err)
            await self._write_silence()
            return

        await self.write_event(
            AudioStart(
                rate=TTS_SAMPLE_RATE, width=TTS_SAMPLE_WIDTH, channels=TTS_CHANNELS
            ).event()
        )
        for offset in range(0, len(audio), _CHUNK_BYTES):
            await self.write_event(
                AudioChunk(
                    rate=TTS_SAMPLE_RATE,
                    width=TTS_SAMPLE_WIDTH,
                    channels=TTS_CHANNELS,
                    audio=audio[offset : offset + _CHUNK_BYTES],
                ).event()
            )
        await self.write_event(AudioStop().event())
        logger.info("Spoke %d bytes for: %s", len(audio), text[:80])

    async def _write_silence(self) -> None:
        """Close the stream cleanly so Assist does not wait for audio forever."""
        await self.write_event(
            AudioStart(
                rate=TTS_SAMPLE_RATE, width=TTS_SAMPLE_WIDTH, channels=TTS_CHANNELS
            ).event()
        )
        await self.write_event(AudioStop().event())

    def _fetch_tts(self, text: str) -> bytes:
        """Ask Jarvis's own /api/tts, so voice and fallbacks stay in one place."""
        url = self._settings["tts_url"]
        payload = json.dumps({"text": text}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = self._settings.get("api_token")
        if token:
            headers["Authorization"] = "Bearer " + token

        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._settings["tts_timeout"]) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError(f"TTS {err.code}: {detail}") from err
