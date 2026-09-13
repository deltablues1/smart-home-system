"""
Shared audio ingress service for Telegram and Raspberry Pi voice paths.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Dict, Optional


SUPPORTED_AUDIO_MIME_TYPES = {
    "audio/ogg",
    "audio/opus",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/m4a",
    "audio/webm",
}

MAX_AUDIO_BYTES = 20 * 1024 * 1024


class AudioIngressError(RuntimeError):
    pass


@dataclass
class AudioIngressResult:
    transcript: str
    metadata: Dict[str, object] = field(default_factory=dict)


class AudioIngressService:
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model or os.getenv("AUDIO_TRANSCRIPTION_MODEL", "gemini-3.5-flash")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        mime_type: str,
        source: str,
        metadata: Optional[Dict[str, object]] = None,
    ) -> AudioIngressResult:
        metadata = dict(metadata or {})
        mime_type = (mime_type or "").strip().lower()

        if not audio_bytes:
            raise AudioIngressError("Audio payload is empty")
        if mime_type not in SUPPORTED_AUDIO_MIME_TYPES:
            raise AudioIngressError(f"Unsupported audio MIME type: {mime_type}")
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise AudioIngressError(f"Audio payload exceeds {MAX_AUDIO_BYTES} bytes inline limit")

        # Engines implemented by STTService (single shared implementation for
        # every voice path): Chirp — native per-language ASR, far more reliable
        # for Croatian than Gemini multimodal; and the opt-in OpenAI
        # gpt-4o-transcribe path. Keep this set in sync with
        # services/audio/stt_service.py STTService.transcribe.
        _stt_service_engines = {"chirp", "chirp_2", "chirp2", "cloud", "openai"}

        engine = os.getenv("STT_ENGINE", "gemini").strip().lower()
        if engine in _stt_service_engines:
            from services.audio.stt_service import STTService

            transcript = await STTService().transcribe(audio_bytes, mime_type)
        elif engine in {"gemini", ""}:
            if not self.api_key:
                raise AudioIngressError("GEMINI_API_KEY is not configured")
            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                None,
                self._transcribe_sync,
                audio_bytes,
                mime_type,
            )
        else:
            # Fail loudly: an unknown value used to fall into the Gemini
            # branch silently and transcribe with the wrong engine.
            raise AudioIngressError(
                f"Unsupported STT_ENGINE '{engine}'. "
                "Supported: chirp (chirp_2/cloud), openai or gemini."
            )
        transcript = (transcript or "").strip()
        if not transcript:
            raise AudioIngressError("Model returned an empty transcript")

        metadata.update({
            "source": source,
            "mime_type": mime_type,
            "bytes": len(audio_bytes),
            # Engine-aware: reporting the Gemini model while Chirp/OpenAI did
            # the transcription was misleading in logs.
            "engine": engine or "gemini",
            "model": self.model if engine in {"gemini", ""} else f"stt:{engine}",
        })
        return AudioIngressResult(transcript=transcript, metadata=metadata)

    def _transcribe_sync(self, audio_bytes: bytes, mime_type: str) -> str:
        from google import genai
        from google.genai import types

        # Transcribe via Vertex AI using the service account (project quota),
        # not the Developer API key — that key routes to a billing/quota that
        # can be depleted (RESOURCE_EXHAUSTED) or blocked for Vertex. Remove the
        # api key from env so the Vertex client authenticates via ADC/SA and
        # reads GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION (global) just like the
        # ADK chat path that is already working.
        _kkeys = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
        _kbackup = {k: os.environ.pop(k) for k in _kkeys if k in os.environ}
        try:
            client = genai.Client(vertexai=True)
        finally:
            os.environ.update(_kbackup)
        response = client.models.generate_content(
            model=self.model,
            contents=[
                "Transcribe the spoken audio faithfully. Return only the transcript text.",
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            ],
        )
        return getattr(response, "text", "") or ""


_audio_ingress_service: AudioIngressService | None = None


def get_supported_audio_mime_types() -> set[str]:
    return set(SUPPORTED_AUDIO_MIME_TYPES)


def get_audio_ingress_service() -> AudioIngressService:
    global _audio_ingress_service
    if _audio_ingress_service is None:
        _audio_ingress_service = AudioIngressService()
    return _audio_ingress_service
