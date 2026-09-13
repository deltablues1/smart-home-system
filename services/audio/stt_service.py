"""
Speech-to-Text Service
======================
Shared STT via Gemini multimodal. Used by:
  - Telegram voice handler (OGG/Opus -> text)
  - Wake word interface (PCM16 -> text)

Uses Gemini Flash with Google AI API key (same as TTS endpoint).
"""

import asyncio
import logging
import os

from config.google_runtime import (
    genai_vertex_env_keys,
    get_gemini_location,
    get_google_api_key,
    get_google_cloud_project,
)
from services.google_retry import run_with_bounded_retry
from tools.resilience.retry_handler import RetryConfig

logger = logging.getLogger(__name__)

# Supported MIME types for Gemini audio input
SUPPORTED_MIME_TYPES = {
    "audio/ogg",        # Telegram voice messages (OGG/Opus)
    "audio/wav",        # WAV files
    "audio/mpeg",       # MP3
    "audio/webm",       # WebM audio
    "audio/x-wav",      # Alternative WAV
    "audio/L16",        # Raw PCM16
}


def _stt_use_vertex() -> bool:
    override = os.getenv("STT_USE_VERTEXAI", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# --- Gemini 3.5 Transcribe (Interactions API) -----------------------------
# A purpose-built ASR model rather than the multimodal path below: the audio
# goes inline (no Files API round trip) and the language is pinned, which is
# the whole reason Croatian ended up on Chirp in the first place.
#
# Developer API only, verified 2026-09-02: the publisher model is not on Vertex
# (404 in both `global` and `us-central1`) and the SDK refuses `interactions`
# on a Vertex client outright. So this path builds its own client with the API
# key even though everything else in the process talks to Vertex.
_INTERACTIONS_MIME = {
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/ogg": "audio/ogg",
    "audio/opus": "audio/opus",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mp3",
    "audio/m4a": "audio/m4a",
    "audio/mp4": "audio/m4a",
    "audio/flac": "audio/flac",
    "audio/aac": "audio/aac",
    "audio/l16": "audio/l16",
}

_GEMINI_TRANSCRIBE_ENGINES = {"gemini_transcribe", "gemini-transcribe", "transcribe"}


def post_interaction(payload: dict, timeout: float = 30.0) -> dict:
    """POST one Interactions request and return the parsed response.

    Raw REST rather than the SDK on purpose. google-genai 1.x still speaks the
    pre-May-2026 Interactions schema and the API answers it with HTTP 400; the
    2.x line fixes that, but this process shares its google-genai with ADK
    1.31.1 and every Vertex agent in the house. One `urllib` call has no such
    entanglement — the same reasoning that put Chirp on REST above.
    """
    import json as _json
    import urllib.error
    import urllib.request

    api_key = get_google_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY / GOOGLE_API_KEY not set - required for STT_ENGINE=gemini_transcribe"
        )
    endpoint = (
        os.getenv("GEMINI_TRANSCRIBE_ENDPOINT", "").strip()
        or "https://generativelanguage.googleapis.com/v1beta/interactions"
    )
    request = urllib.request.Request(
        endpoint,
        data=_json.dumps(payload).encode("utf-8"),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # The body carries the reason that matters — a depleted prepayment
        # balance reads as a bare 429 without it.
        detail = err.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"Interactions API {err.code}: {detail}") from err


# --- Cloud Speech-to-Text v2 (Chirp) credentials --------------------------
# Cloud STT uses native per-language ASR (Chirp), far more reliable for
# Croatian than Gemini multimodal, especially on short utterances. We call the
# REST API with the service-account token via google.auth (no extra dependency).
_cloud_stt_creds = None


def _cloud_stt_access_token() -> str:
    global _cloud_stt_creds
    import google.auth
    from google.auth.transport.requests import Request as _AuthRequest

    if _cloud_stt_creds is None:
        _cloud_stt_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _cloud_stt_creds.valid:
        _cloud_stt_creds.refresh(_AuthRequest())
    return _cloud_stt_creds.token


class STTService:
    """Speech-to-Text via Gemini multimodal audio input."""

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("STT_MODEL", "gemini-2.5-flash")
        self.default_language = os.getenv("STT_LANGUAGE", "Croatian").strip() or "Croatian"
        # STT engine: "gemini" (default, multimodal) or "chirp"/"cloud"
        # (Cloud Speech-to-Text v2, native Croatian ASR).
        self.engine = os.getenv("STT_ENGINE", "gemini").strip().lower()
        self.cloud_model = os.getenv("STT_CLOUD_MODEL", "chirp_2").strip() or "chirp_2"
        self.cloud_location = os.getenv("STT_CLOUD_LOCATION", "us-central1").strip() or "us-central1"
        self.cloud_language_code = os.getenv("STT_LANGUAGE_CODE", "hr-HR").strip() or "hr-HR"

    def _transcribe_cloud_sync(self, audio_bytes: bytes) -> str:
        """Transcribe via Cloud Speech-to-Text v2 (Chirp). Returns plain text."""
        import base64
        import json as _json
        import urllib.request

        project = get_google_cloud_project(required=True)
        location = self.cloud_location
        url = (
            "https://%s-speech.googleapis.com/v2/projects/%s/locations/%s"
            "/recognizers/_:recognize" % (location, project, location)
        )
        payload = {
            "config": {
                "model": self.cloud_model,
                "languageCodes": [self.cloud_language_code],
                "features": {"enableAutomaticPunctuation": True},
                "autoDecodingConfig": {},
            },
            "content": base64.b64encode(audio_bytes).decode("ascii"),
        }
        req = urllib.request.Request(
            url,
            data=_json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % _cloud_stt_access_token(),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        parts = []
        for result in body.get("results", []):
            alternatives = result.get("alternatives") or []
            if alternatives and alternatives[0].get("transcript"):
                parts.append(alternatives[0]["transcript"].strip())
        return " ".join(parts).strip()

    def _openai_model(self) -> str:
        return os.getenv("OPENAI_STT_MODEL", "").strip() or "gpt-4o-transcribe"

    def _transcribe_openai_sync(self, audio_bytes: bytes, mime_type: str) -> str:
        """Transcribe via OpenAI audio transcriptions. Returns plain text."""
        import io
        from openai import OpenAI

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set - required for STT_ENGINE=openai")
        client = OpenAI(api_key=key)

        # Keep in sync with services/audio_ingress.py SUPPORTED_AUDIO_MIME_TYPES
        # — a wrong extension makes OpenAI misparse the container.
        ext = {
            "audio/ogg": "ogg",
            "audio/opus": "ogg",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/mp4": "mp4",
            "audio/m4a": "m4a",
            "audio/webm": "webm",
            "audio/L16": "wav",
        }.get(mime_type, "wav")
        buf = io.BytesIO(audio_bytes)
        buf.name = "audio.%s" % ext

        # ISO-639-1 code; default Croatian. Overridable via OPENAI_STT_LANGUAGE.
        lang = os.getenv("OPENAI_STT_LANGUAGE", "").strip() or "hr"
        resp = client.audio.transcriptions.create(
            model=self._openai_model(),
            file=buf,
            language=lang,
        )
        return (getattr(resp, "text", "") or "").strip()

    def _transcribe_model(self) -> str:
        return os.getenv("GEMINI_TRANSCRIBE_MODEL", "").strip() or "gemini-3.5-transcribe"

    def _transcribe_config(self) -> dict:
        """The transcription_config sent with every Interactions request."""
        raw = os.getenv("GEMINI_TRANSCRIBE_LANGUAGES", "").strip() or self.cloud_language_code
        # Never left empty: an empty list means "detect the language", and
        # detection is exactly what mistook Croatian for Macedonian and Czech
        # on the two engines that were rejected before this one.
        config: dict = {"language_codes": [c.strip() for c in raw.split(",") if c.strip()]}

        mode = os.getenv("GEMINI_TRANSCRIBE_MODE", "").strip().lower()
        if mode in {"smart", "verbatim"}:
            config["mode"] = {"type": mode}

        vocabulary = [
            term.strip()
            for term in os.getenv("GEMINI_TRANSCRIBE_VOCABULARY", "").split(",")
            if term.strip()
        ]
        if vocabulary:
            # The API caps the list at 1000 terms and rejects the whole request
            # if it is longer, so the cut happens here rather than there.
            config["custom_vocabulary"] = vocabulary[:1000]
        return config

    def _transcribe_interactions_sync(self, audio_bytes: bytes, mime_type: str) -> str:
        """Transcribe via Gemini 3.5 Transcribe. Returns plain text."""
        import base64

        mime = _INTERACTIONS_MIME.get((mime_type or "").strip().lower())
        if not mime:
            raise RuntimeError(
                f"{mime_type} is not an audio type {self._transcribe_model()} accepts"
            )

        item: dict = {
            "type": "audio",
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "mime_type": mime,
        }
        if mime == "audio/l16":
            # Raw PCM carries no header, so the rate travels beside it.
            item["rate"] = int(os.getenv("STT_PCM_SAMPLE_RATE", "16000"))
            item["channels"] = 1

        # Input items are wrapped in a turn — the May 2026 schema change that
        # the older SDK still gets wrong.
        payload = {
            "model": self._transcribe_model(),
            "input": [{"type": "user_input", "content": [item]}],
            "generation_config": {"transcription_config": self._transcribe_config()},
        }
        body = post_interaction(
            payload,
            timeout=float(os.getenv("GEMINI_TRANSCRIBE_TIMEOUT_SECONDS", "30")),
        )

        text = body.get("output_text")
        if not text:
            # Same words, one level down; kept as a fallback so a response
            # shape that drops the convenience field is not silence.
            text = " ".join(
                part.get("text", "")
                for step in body.get("steps", [])
                for part in step.get("content", [])
                if part.get("type") == "text"
            )
        return (text or "").strip()

    def _get_client(self):
        from google import genai as _genai

        if _stt_use_vertex():
            project = get_google_cloud_project(required=True)
            location = get_gemini_location(default="global")
            # If an API key is present in the env, google-genai would attach it
            # to Vertex requests ("express mode"), which fails when the key is
            # not enabled for aiplatform.googleapis.com (API_KEY_SERVICE_BLOCKED).
            # Remove it during construction so the client authenticates via the
            # service account (ADC), exactly like the ADK agent path.
            _kkeys = ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
            _kbackup = {k: os.environ.pop(k) for k in _kkeys if k in os.environ}
            try:
                return _genai.Client(vertexai=True, project=project, location=location)
            finally:
                os.environ.update(_kbackup)

        api_key = get_google_api_key()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY not set - required for STT")

        # Temporarily disable Vertex AI env vars (same pattern as TTS in web/app.py)
        _vkeys = genai_vertex_env_keys()
        _backup = {k: os.environ.pop(k) for k in _vkeys if k in os.environ}
        try:
            client = _genai.Client(api_key=api_key)
        finally:
            os.environ.update(_backup)
        return client

    async def transcribe(
        self,
        audio_bytes: bytes,
        mime_type: str = "audio/ogg",
        language: str | None = None,
    ) -> str:
        """
        Transcribe audio to text using Gemini multimodal.

        Args:
            audio_bytes: Raw audio data
            mime_type: Audio MIME type (audio/ogg, audio/wav, etc.)
            language: Target language for transcription. Falls back to STT_LANGUAGE env.

        Returns:
            Transcribed text string

        Raises:
            RuntimeError: If API key missing or transcription fails
            ValueError: If audio is empty or mime_type unsupported
        """
        if not audio_bytes:
            raise ValueError("Empty audio data")

        if mime_type not in SUPPORTED_MIME_TYPES:
            logger.warning(f"Unsupported MIME type {mime_type}, attempting anyway")

        # OpenAI STT path (opt-in via STT_ENGINE=openai). gpt-4o-transcribe has
        # strong Croatian accuracy; the Gemini/Chirp defaults are untouched.
        if self.engine == "openai":
            async def _call_openai():
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_openai_sync, audio_bytes, mime_type
                )

            transcript = await run_with_bounded_retry(
                "stt_openai_transcribe",
                _call_openai,
                config=RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0),
                log=logger,
            )
            logger.info(
                f"STT[openai:{self._openai_model()}] transcription "
                f"({len(audio_bytes)} bytes): {transcript[:100]}"
            )
            return transcript

        # Gemini 3.5 Transcribe (opt-in via STT_ENGINE=gemini_transcribe).
        if self.engine in _GEMINI_TRANSCRIBE_ENGINES:
            async def _call_transcribe():
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_interactions_sync, audio_bytes, mime_type
                )

            try:
                transcript = await run_with_bounded_retry(
                    "stt_gemini_transcribe",
                    _call_transcribe,
                    config=RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0),
                    log=logger,
                )
            except Exception as err:  # noqa: BLE001 - hearing matters more
                fallback = os.getenv("STT_FALLBACK_ENGINE", "chirp").strip().lower()
                if fallback in {"", "none", "off"}:
                    raise
                # A depleted balance, a rotated key or a five-minute outage at
                # Google must not leave the house deaf: Chirp still works and
                # costs the same as it did before this engine existed.
                logger.warning(
                    "STT[%s] failed (%s); falling back to %s",
                    self._transcribe_model(), err, self.cloud_model,
                )
                transcript = await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_cloud_sync, audio_bytes
                )
                logger.info(
                    f"STT[{self.cloud_model} fallback] transcription "
                    f"({len(audio_bytes)} bytes): {transcript[:100]}"
                )
                return transcript

            logger.info(
                f"STT[{self._transcribe_model()}] transcription "
                f"({len(audio_bytes)} bytes): {transcript[:100]}"
            )
            return transcript

        # Native Cloud STT (Chirp) path — native Croatian ASR.
        if self.engine in {"chirp", "chirp_2", "chirp2", "cloud"}:
            async def _call_cloud():
                return await asyncio.get_event_loop().run_in_executor(
                    None, self._transcribe_cloud_sync, audio_bytes
                )

            transcript = await run_with_bounded_retry(
                "stt_cloud_recognize",
                _call_cloud,
                config=RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0),
                log=logger,
            )
            logger.info(
                f"STT[{self.cloud_model}] transcription ({len(audio_bytes)} bytes): {transcript[:100]}"
            )
            return transcript

        client = self._get_client()
        from google.genai import types as _types
        target_language = (language or self.default_language).strip() or self.default_language

        prompt = (
            f"Transcribe this {target_language} audio message exactly as spoken. "
            f"The speaker is speaking {target_language}; do NOT interpret it as a "
            "different language (e.g. Polish, Russian, Serbian or Slovenian) and "
            "do NOT translate it. Preserve native orthography and diacritics "
            "(for Croatian: č, ć, š, ž, đ). "
            "Return ONLY the transcription text, nothing else. "
            "If the audio is unclear or empty, return '[nečujno]'."
        )

        audio_part = _types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        text_part = _types.Part.from_text(text=prompt)

        try:
            async def _call_model():
                return await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=self.model,
                        contents=[audio_part, text_part],
                    ),
                )

            response = await run_with_bounded_retry(
                "stt_transcribe",
                _call_model,
                config=RetryConfig(max_retries=1, base_delay=1.5, max_delay=6.0),
                log=logger,
            )
            transcript = response.text.strip()
            logger.info(f"STT transcription ({len(audio_bytes)} bytes): {transcript[:100]}...")
            return transcript

        except Exception as e:
            logger.error(f"STT transcription failed: {e}")
            raise RuntimeError(f"Transcription failed: {e}") from e

    async def transcribe_pcm16(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> str:
        """
        Transcribe raw PCM16 audio (from microphone) to text.

        Wraps PCM bytes in a WAV container before sending to Gemini.

        Args:
            pcm_bytes: Raw PCM16 little-endian audio data
            sample_rate: Sample rate in Hz (default 16000)
            language: Target language. Falls back to STT_LANGUAGE env.

        Returns:
            Transcribed text string
        """
        import io
        import wave

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)

        wav_bytes = wav_buffer.getvalue()
        return await self.transcribe(wav_bytes, "audio/wav", language)
