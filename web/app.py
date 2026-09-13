"""
FastAPI application for Google Workspace ADK Web Interface.

Provides REST API endpoints and serves the operational dashboard.
Uses lifespan events for proper async initialization (APScheduler needs event loop).
"""

import os
import json
import base64
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from sse_starlette.sse import EventSourceResponse

from web.models import (
    ChatRequest, ChatResponse, SessionInfo,
    AgentInfo, TraceEvent,
)
from config.agent_registry import get_agent_config, get_worker_agent_names
from config.deployment_config import get_deployment_config
from config.google_runtime import (
    genai_vertex_env_keys,
    get_gemini_location,
    get_google_api_key,
    get_google_cloud_project,
)
from monitoring.metrics import get_metrics_collector
from services.google_retry import classify_google_runtime_error, run_with_bounded_retry
from tools.resilience.retry_handler import RetryConfig

logger = logging.getLogger(__name__)
_metrics = get_metrics_collector()

# Will be set by create_app()
_web_interface = None

# ---------------------------------------------------------------------------
# In-memory cost & quota tracker (resets on server restart)
# ---------------------------------------------------------------------------
from collections import defaultdict
import time as _time

_cost_tracker = {
    "start_time": _time.time(),
    # TTS — Gemini 2.5 Flash TTS (preview, besplatno dok traje)
    "tts_calls": 0,
    "tts_chars": 0,
    "tts_cost_usd": 0.0,
    # Generative AI calls tracked via token counts
    "llm_calls": defaultdict(int),       # model -> count
    "llm_input_tokens": defaultdict(int), # model -> tokens
    "llm_output_tokens": defaultdict(int),# model -> tokens
    "llm_cost_usd": defaultdict(float),   # model -> USD
    # Veo / Imagen
    "veo_calls": 0,
    "veo_cost_usd": 0.0,
    "imagen_calls": 0,
    "imagen_cost_usd": 0.0,
    # 429 errors
    "quota_errors": defaultdict(int),
    "quota_errors_last": defaultdict(float),
    # Per-session spend (last 20 sessions)
    "session_costs": [],
}

# Pricing (USD) — Vertex AI Gemini and media, April 2026.
#
# LLM token spend is NOT tracked here: _track_llm below has no callers, and its
# _PRICING.get(key, (0, 0)) fallback would price every Claude call at zero
# anyway. The live token accounting is tools/observability/token_stats.py
# (behind /tokens) — fix prices there, not here.
_PRICING = {
    # model_key: (input_per_1M, output_per_1M)
    "gemini-3.5-flash":    (0.10,  0.40),  # GA default workhorse
    "gemini-3.1-flash-lite": (0.075, 0.30),  # GA cheapest
    "gemini-3-flash":      (0.10,  0.40),
    "gemini-3.1-pro":      (3.50, 10.50),  # gemini-3.1-pro-preview (approx, preview pricing)
    "gemini-2.5-pro":      (3.50, 10.50),
    "gemini-2.5-flash":    (0.15,  0.60),
    "gemini-2.5-flash-lite": (0.075, 0.30),
    "tts":                 (0.075 / 1_000_000, 0),  # per char
    "veo3":                (0.40, 0),                # per video
    "imagen3":             (0.04, 0),                # per image
}

def _model_key(model_name: str) -> str:
    m = model_name.lower()
    if "3.1-flash-lite" in m: return "gemini-3.1-flash-lite"
    if "3.5-flash" in m:   return "gemini-3.5-flash"
    if "3.1-pro" in m:     return "gemini-3.1-pro"
    if "3-flash" in m or "3flash" in m: return "gemini-3-flash"
    if "2.5-pro" in m:     return "gemini-2.5-pro"
    if "2.5-flash-lite" in m: return "gemini-2.5-flash-lite"
    if "2.5-flash" in m:   return "gemini-2.5-flash"
    return m

def _track_tts(char_count: int):
    _cost_tracker["tts_calls"] += 1
    _cost_tracker["tts_chars"] += char_count
    # TTS preview = besplatno, ali pratimo za kad krene naplaćivati
    _cost_tracker["tts_cost_usd"] += char_count * _PRICING["tts"][0]

def _track_llm(model: str, input_tokens: int, output_tokens: int):
    key = _model_key(model)
    p = _PRICING.get(key, (0, 0))
    cost = (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000
    _cost_tracker["llm_calls"][key] += 1
    _cost_tracker["llm_input_tokens"][key] += input_tokens
    _cost_tracker["llm_output_tokens"][key] += output_tokens
    _cost_tracker["llm_cost_usd"][key] += cost

def _track_veo():
    _cost_tracker["veo_calls"] += 1
    _cost_tracker["veo_cost_usd"] += _PRICING["veo3"][0]

def _track_imagen():
    _cost_tracker["imagen_calls"] += 1
    _cost_tracker["imagen_cost_usd"] += _PRICING["imagen3"][0]

def _track_quota_error(service: str):
    _cost_tracker["quota_errors"][service] += 1
    _cost_tracker["quota_errors_last"][service] = _time.time()


def _default_tts_voice_name() -> str:
    return "Charon" if os.environ.get("DEPLOYMENT_PROFILE") == "rpi-home" else "Aoede"


def _voice_assistant_gender() -> str:
    gender = os.environ.get("VOICE_ASSISTANT_GENDER", "").strip().lower()
    if gender in {"male", "muski", "muški"}:
        return "male"
    if gender in {"female", "zenski", "ženski"}:
        return "female"
    return "male" if os.environ.get("DEPLOYMENT_PROFILE") == "rpi-home" else "female"


def _default_tts_model() -> str:
    explicit = os.environ.get("TTS_MODEL", "").strip()
    if explicit:
        return explicit
    # The Gemini Developer API exposes TTS under the "-preview-" name (v1beta);
    # Vertex AI uses the GA name. Pick the right default for the active backend.
    return "gemini-2.5-flash-tts" if _tts_use_vertex() else "gemini-2.5-flash-preview-tts"


def _tts_language_code() -> str:
    return os.environ.get("TTS_LANGUAGE_CODE", "").strip()


def _tts_use_vertex() -> bool:
    override = os.environ.get("TTS_USE_VERTEXAI", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_tts_api_key() -> str:
    return get_google_api_key()


# --- OpenAI TTS engine (opt-in via TTS_ENGINE=openai) ---------------------
# Additive: the default Gemini/Cloud paths are untouched. When TTS_ENGINE is
# "openai", /api/tts routes here. OpenAI's response_format="pcm" returns raw
# 24 kHz / 16-bit / mono PCM — exactly the format the existing player expects
# (audio/L16;rate=24000), so no resampling or container handling is needed.
_openai_client = None


def _tts_engine() -> str:
    return os.environ.get("TTS_ENGINE", "").strip().lower()


def _openai_api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _openai_tts_model() -> str:
    return os.environ.get("OPENAI_TTS_MODEL", "").strip() or "gpt-4o-mini-tts"


def _openai_tts_voice() -> str:
    # "alloy" is a known-good default; set OPENAI_TTS_VOICE to a business voice
    # (e.g. "marin"/"cedar") once confirmed available on the account.
    return os.environ.get("OPENAI_TTS_VOICE", "").strip() or "alloy"


# Steers gpt-4o-mini-tts delivery. Croatian by default because the assistant
# answers in Croatian; without it the voice tends to drift into an English
# accent on Croatian text. Override via OPENAI_TTS_INSTRUCTIONS; set to "none"
# to send no instructions at all.
_DEFAULT_OPENAI_TTS_INSTRUCTIONS = (
    "Govori na hrvatskom jeziku s prirodnim hrvatskim izgovorom. "
    "Ton: topao, smiren i samouvjeren kućni asistent. "
    "Tempo: umjeren, prirodan govorni ritam; kratke stanke na zarezima. "
    "Brojeve, datume i mjerne jedinice izgovaraj prirodno na hrvatskom."
)


def _openai_tts_instructions() -> Optional[str]:
    raw = os.environ.get("OPENAI_TTS_INSTRUCTIONS", "").strip()
    if raw.lower() == "none":
        return None
    return raw or _DEFAULT_OPENAI_TTS_INSTRUCTIONS


def _tts_fallback_voice() -> str:
    """Cloud TTS voice used when OpenAI TTS fails (empty string disables)."""
    return os.environ.get(
        "TTS_FALLBACK_VOICE", "hr-HR-Chirp3-HD-Charon"
    ).strip()


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        key = _openai_api_key()
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set — required for TTS_ENGINE=openai")
        _openai_client = OpenAI(api_key=key)
    return _openai_client


def _openai_tts_kwargs(text: str, voice_name: str) -> dict:
    kwargs = {
        "model": _openai_tts_model(),
        "voice": voice_name or _openai_tts_voice(),
        "input": text,
        "response_format": "pcm",
    }
    instructions = _openai_tts_instructions()
    if instructions:
        kwargs["instructions"] = instructions
    return kwargs


def _synthesize_openai_tts_pcm(text: str, voice_name: str) -> bytes:
    """Synthesize via OpenAI TTS and return raw PCM16 @ 24 kHz mono."""
    client = _get_openai_client()
    resp = client.audio.speech.create(**_openai_tts_kwargs(text, voice_name))
    # openai 2.x returns a binary response wrapper; .read() yields the bytes.
    if hasattr(resp, "read"):
        return resp.read()
    return resp.content


def _stream_openai_tts_pcm(text: str, voice_name: str):
    """Yield PCM16 @ 24 kHz chunks as OpenAI synthesizes them.

    Cuts time-to-first-sound from (full synthesis + download) to the first
    chunk. The OpenAI call is created lazily inside the generator, so a
    connection-level failure surfaces on the first next() — the /api/tts
    handler probes that before committing to a streamed response.
    """
    client = _get_openai_client()
    with client.audio.speech.with_streaming_response.create(
        **_openai_tts_kwargs(text, voice_name)
    ) as resp:
        # 4800 B = 100 ms of PCM16 @ 24 kHz mono — small enough for snappy
        # playback start, large enough to keep per-chunk overhead low.
        for chunk in resp.iter_bytes(chunk_size=4800):
            if chunk:
                yield chunk


def _sanitize_tts_text(text: str) -> str:
    import re

    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    # Emoji and pictographs — TTS engines read them out loud ("smiling face…").
    text = re.sub(
        "["
        "\U0001F300-\U0001FAFF"  # symbols, pictographs, emoticons, extended
        "\U00002600-\U000027BF"  # misc symbols + dingbats
        "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
        "\U00002B00-\U00002BFF"  # arrows/symbols block used by some emoji
        "\uFE0F\u200D"           # variation selector + zero-width joiner
        "]+",
        "",
        text,
    )
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    return text.strip()[:4000]


def _create_tts_client():
    from google import genai as _genai

    use_vertex = _tts_use_vertex()
    project = get_google_cloud_project(required=False)
    location = get_gemini_location(default="global")

    if use_vertex and project:
        return _genai.Client(vertexai=True, project=project, location=location)

    api_key = _get_tts_api_key()
    if not api_key:
        raise RuntimeError("Neither Vertex AI config nor GEMINI_API_KEY/GOOGLE_API_KEY is set")

    _vkeys = genai_vertex_env_keys()
    _backup = {k: os.environ.pop(k) for k in _vkeys if k in os.environ}
    try:
        return _genai.Client(api_key=api_key)
    finally:
        os.environ.update(_backup)


def _build_tts_config(voice_name: str):
    from google.genai import types as _types

    speech_config = _types.SpeechConfig(
        voice_config=_types.VoiceConfig(
            prebuilt_voice_config=_types.PrebuiltVoiceConfig(voice_name=voice_name)
        )
    )
    language_code = _tts_language_code()
    if language_code:
        speech_config.language_code = language_code

    return _types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=speech_config,
    )


def _extract_pcm_from_tts_response(response) -> bytes:
    return response.candidates[0].content.parts[0].inline_data.data


# --- Cloud Text-to-Speech (Chirp3-HD native voices) ----------------------
# Used when the requested voice is a full Cloud TTS voice id such as
# "hr-HR-Chirp3-HD-Charon". These are native, per-language voices and sound far
# more natural for Croatian than the Gemini prebuilt voices (which speak
# Croatian with a foreign accent). We call the REST API with the service-account
# token via google.auth (no extra dependency) and return raw PCM16 @ 24 kHz to
# match the existing player path. Gemini voices (bare names like "Charon") keep
# using the original generate_content path untouched.
_cloud_tts_creds = None


def _is_cloud_tts_voice(voice_name: str) -> bool:
    import re

    name = (voice_name or "").strip()
    # Cloud TTS ids look like "<lang>-<REGION>-..."; Gemini voices are bare names.
    return bool(re.match(r"^[a-z]{2,3}-[A-Z]{2}-", name))


def _cloud_tts_language_code(voice_name: str) -> str:
    import re

    match = re.match(r"^([a-z]{2,3}-[A-Z]{2})-", voice_name or "")
    if match:
        return match.group(1)
    return _tts_language_code() or "hr-HR"


def _cloud_access_token() -> str:
    global _cloud_tts_creds
    import google.auth
    from google.auth.transport.requests import Request as _AuthRequest

    if _cloud_tts_creds is None:
        _cloud_tts_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _cloud_tts_creds.valid:
        _cloud_tts_creds.refresh(_AuthRequest())
    return _cloud_tts_creds.token


def _synthesize_cloud_tts_pcm(text: str, voice_name: str) -> bytes:
    """Synthesize via Cloud TTS REST and return raw PCM16 @ 24 kHz mono."""
    import base64
    import io
    import json as _json
    import urllib.request
    import wave

    payload = {
        "input": {"text": text},
        "voice": {
            "languageCode": _cloud_tts_language_code(voice_name),
            "name": voice_name,
        },
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000},
    }
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % _cloud_access_token(),
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = _json.loads(resp.read().decode("utf-8"))
    audio_b64 = body.get("audioContent")
    if not audio_b64:
        raise RuntimeError("Cloud TTS returned no audioContent")
    # LINEAR16 comes back as a WAV container; return the raw PCM frames only.
    with wave.open(io.BytesIO(base64.b64decode(audio_b64)), "rb") as wav:
        return wav.readframes(wav.getnframes())


def _iter_tts_stream_chunks(text: str, voice_name: str, model_name: str):
    config = _build_tts_config(voice_name)
    retry_config = RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0)
    last_error = None

    for attempt in range(retry_config.max_retries + 1):
        client = _create_tts_client()
        yielded_any = False
        try:
            for chunk in client.models.generate_content_stream(
                model=model_name,
                contents=text,
                config=config,
            ):
                if (
                    chunk.candidates is None
                    or not chunk.candidates
                    or chunk.candidates[0].content is None
                    or not chunk.candidates[0].content.parts
                ):
                    continue

                part = chunk.candidates[0].content.parts[0]
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    yielded_any = True
                    yield inline.data
            return
        except Exception as exc:
            last_error = exc
            error_type, retryable, is_quota = classify_google_runtime_error(exc)
            if yielded_any or attempt >= retry_config.max_retries or not retryable:
                raise
            delay = min(4.0, (retry_config.base_delay * (2 ** attempt)))
            logger.warning(
                "tts_stream_init failed (%s, quota=%s) attempt %s/%s; retrying in %.2fs",
                error_type,
                is_quota,
                attempt + 1,
                retry_config.max_retries + 1,
                delay,
            )
            _time.sleep(delay)

    if last_error is not None:
        raise last_error


def _build_live_system_prompt() -> str:
    if _voice_assistant_gender() == "male":
        return (
            "Ti si glasovni asistent integriran u Google Workspace poslovnu platformu. "
            "Odgovaraj kratko i jasno — razgovaramo glasom, ne pišemo. "
            "Ako te pitaju o emailovima, kalendarima, dokumentima ili zadacima, reci da to nije dostupno "
            "u glasovnom načinu rada — korisnik mora upisati poruku za agente. "
            "Govori prirodno, u jednoj ili dvije rečenice. Izbjegavaj nabrajanje i dugačke liste. "
            "Uvijek govori o sebi u muškom rodu."
        )
    return (
        "Ti si glasovna asistentica integrirana u Google Workspace poslovnu platformu. "
        "Odgovaraj kratko i jasno — razgovaramo glasom, ne pišemo. "
        "Ako te pitaju o emailovima, kalendarima, dokumentima ili zadacima, reci da to nije dostupno "
        "u glasovnom načinu rada — korisnik mora upisati poruku za agente. "
        "Govori prirodno, u jednoj ili dvije rečenice. Izbjegavaj nabrajanje i dugačke liste. "
        "Uvijek govori o sebi u ženskom rodu."
    )

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Cap how often one caller can start paid work.

    The daily budget stops spending after the fact; this is what keeps a device
    on the LAN from reaching that ceiling in a minute. False wake-word triggers
    drained the credit overnight on 2026-08-30 without anything on this side
    counting the requests.

    Only the routes that cost money are limited — reading sessions, status and
    metrics stay unthrottled so a dashboard cannot lock you out of your own
    diagnostics.
    """

    PAID_PREFIXES = ("/api/chat", "/api/tts", "/api/upload", "/api/live")

    def __init__(self, app, limit: int, window_seconds: float):
        super().__init__(app)
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, List[float]] = defaultdict(list)

    def _client(self, request: Request) -> str:
        # No proxy in front of the Pi, so the socket address is the caller.
        # X-Forwarded-For is deliberately ignored: it is attacker-controlled
        # and trusting it would make the limit trivially bypassable.
        return getattr(request.client, "host", None) or "unknown"

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self.PAID_PREFIXES):
            return await call_next(request)

        import time as _time

        now = _time.monotonic()
        key = self._client(request)
        recent = [t for t in self._hits[key] if now - t < self.window]

        if len(recent) >= self.limit:
            retry_after = int(self.window - (now - recent[0])) + 1
            self._hits[key] = recent
            logger.warning(
                "Rate limit hit: %s made %d requests to %s within %.0fs",
                key, len(recent), request.url.path, self.window,
            )
            return JSONResponse(
                {
                    "detail": (
                        f"Previše zahtjeva ({len(recent)} u {int(self.window)}s). "
                        "Ovo je zaštita od potrošnje kredita, ne kvar."
                    )
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        recent.append(now)
        self._hits[key] = recent
        if len(self._hits) > 512:  # bounded memory on a long-running Pi
            self._hits = defaultdict(list, {key: recent})
        return await call_next(request)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Simple bearer token auth for /api/* routes (except /api/media/).

    Activated only when API_TOKEN env var is set.
    Local dev without API_TOKEN: no auth required (all requests pass through).
    /api/media/ is intentionally excluded — browser <img>/<video> tags cannot send Bearer headers.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith("/api/media/"):
            import secrets
            auth = request.headers.get("Authorization", "")
            if not secrets.compare_digest(auth, f"Bearer {self.token}"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(interface) -> FastAPI:
    """Create FastAPI app with the given WebInterface instance."""
    global _web_interface
    _web_interface = interface
    deployment_config = get_deployment_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize system inside event loop (APScheduler needs it)."""
        logger.info("Initializing agent system (lifespan startup)...")
        await _web_interface.start()  # Initializes agents + loads sessions from Firestore
        agent_count = len(_web_interface.system.worker_agents)
        logger.info(f"System ready. {agent_count} worker agents loaded.")

        # Seed the approval gate's known recipients from Contacts, so people
        # already in the address book never trigger a mail confirmation.
        # Fire-and-forget: Contacts being unavailable only costs confirmations.
        from services.known_recipients import refresh_from_contacts
        asyncio.create_task(refresh_from_contacts())
        yield
        # Shutdown
        if (_web_interface.system and _web_interface.system.scheduler
                and _web_interface.system.scheduler.scheduler.running):
            _web_interface.system.scheduler.scheduler.shutdown(wait=False)
        await _web_interface.stop()  # Close Firestore connection
        logger.info("Web interface shutdown complete.")

    app = FastAPI(
        title="Google Workspace ADK Dashboard",
        version="1.0.0",
        lifespan=lifespan
    )

    # Optional bearer token auth (set API_TOKEN env var to enable)
    api_token = os.environ.get("API_TOKEN")
    if deployment_config.api_token_required and not api_token:
        raise RuntimeError("API_TOKEN is required by the active deployment profile")
    if api_token:
        app.add_middleware(TokenAuthMiddleware, token=api_token)

    # Registered after the auth middleware so it runs before it: an unauthorised
    # flood should be cheap to refuse.
    try:
        rate_limit = int(os.getenv("API_RATE_LIMIT", "30"))
        rate_window = float(os.getenv("API_RATE_WINDOW_SECONDS", "60"))
    except ValueError:
        rate_limit, rate_window = 30, 60.0
    if rate_limit > 0:
        app.add_middleware(RateLimitMiddleware, limit=rate_limit, window_seconds=rate_window)
        logger.info("Rate limit: %d paid requests per %.0fs per client", rate_limit, rate_window)
        logger.info("API token authentication enabled")
    else:
        logger.warning("API_TOKEN not set — web API is unauthenticated (OK for local dev)")

    # Static files
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir, html=False), name="static")

    # --- Dashboard ---
    @app.get("/")
    async def dashboard():
        return FileResponse(
            os.path.join(static_dir, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )

    # --- Chat endpoints ---
    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        started_at = _time.time()
        try:
            result = await _web_interface.chat(
                user_id=req.user_id,
                message=req.message,
                route_hint=req.route_hint,
                response_mode=req.response_mode,
                session_id=req.session_id,
            )
            duration_s = _time.time() - started_at
            _metrics.record_timing(
                "web_chat_latency",
                duration_s,
                labels={
                    "route": req.route_hint or "auto",
                    "channel": "voice" if req.user_id.startswith(("rpi-voice", "live-voice", "telegram-voice")) else "text",
                },
            )
            _metrics.log_event(
                "web_chat_request",
                {
                    "user_id": req.user_id,
                    "route_hint": req.route_hint,
                    "response_mode": req.response_mode,
                    "message_chars": len(req.message),
                    "response_chars": len(result.get("response", "")),
                    "duration_ms": int(duration_s * 1000),
                    "session_id": result.get("session_id"),
                    "success": True,
                },
            )
            return ChatResponse(**result)
        except Exception as exc:
            duration_s = _time.time() - started_at
            error_text = str(exc)
            error_type, _, is_quota = classify_google_runtime_error(exc)
            if is_quota:
                _track_quota_error("chat")
            _metrics.record_error(
                "web_chat_error",
                labels={"route": req.route_hint or "auto", "error_type": error_type},
            )
            _metrics.log_event(
                "web_chat_request",
                {
                    "user_id": req.user_id,
                    "route_hint": req.route_hint,
                    "response_mode": req.response_mode,
                    "message_chars": len(req.message),
                    "duration_ms": int(duration_s * 1000),
                    "success": False,
                    "error_type": error_type,
                    "quota_error": is_quota,
                    "error": error_text[:300],
                },
            )
            raise

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest):
        # Convert attachments to dicts for web_interface
        attachments = None
        if req.attachments:
            attachments = [att.model_dump() for att in req.attachments]

        async def event_generator():
            async for event in _web_interface.chat_stream(
                user_id=req.user_id,
                message=req.message,
                attachments=attachments,
                session_id=req.session_id,
            ):
                event_type = event.get("event", "message")
                data = event.get("data", "")
                if isinstance(data, dict):
                    data = json.dumps(data, ensure_ascii=False)
                # Track 429/quota errors for monitoring
                if "429" in str(data) or "RESOURCE_EXHAUSTED" in str(data):
                    _track_quota_error("vertex_ai")
                yield {"event": event_type, "data": data}

        return EventSourceResponse(event_generator())

    # --- Gemini TTS endpoint ---
    @app.post("/api/tts")
    async def tts(request: Request):
        """Convert text to speech using Gemini 2.5 Flash TTS. Returns PCM16 audio @ 24kHz."""
        started_at = _time.time()
        body = await request.json()
        text = body.get("text", "").strip()
        voice_name = (body.get("voice_name") or os.environ.get("TTS_VOICE_NAME") or _default_tts_voice_name()).strip()
        stream_audio = bool(body.get("stream"))
        model_name = (body.get("model") or _default_tts_model()).strip()
        if not text:
            raise HTTPException(status_code=400, detail="No text provided")

        text = _sanitize_tts_text(text)

        if not text:
            raise HTTPException(status_code=400, detail="Text is empty after cleanup")

        # OpenAI TTS engine (opt-in via TTS_ENGINE=openai). Voice is controlled by
        # OPENAI_TTS_VOICE (Gemini/Cloud voice ids do not apply here). Returns
        # PCM16 @ 24 kHz to match the existing player path. Default path untouched.
        if _tts_engine() == "openai":
            ov = _openai_tts_voice()

            def _log_openai_success(streamed: bool):
                duration_s = _time.time() - started_at
                _metrics.record_timing(
                    "web_tts_latency",
                    duration_s,
                    labels={"mode": "openai", "vertex": "openai_tts"},
                )
                _metrics.log_event(
                    "web_tts_request",
                    {
                        "voice_name": ov,
                        "model": _openai_tts_model(),
                        "stream": streamed,
                        "vertex": False,
                        "text_chars": len(text),
                        "duration_ms": int(duration_s * 1000),
                        "success": True,
                    },
                )

            # Streaming: send PCM chunks as OpenAI produces them. The first
            # chunk is pulled eagerly so auth/model errors still fall through
            # to the fallback below instead of dying mid-stream.
            if stream_audio:
                try:
                    _track_tts(len(text))
                    chunk_iter = _stream_openai_tts_pcm(text, ov)
                    first_chunk = await asyncio.get_event_loop().run_in_executor(
                        None, next, chunk_iter
                    )

                    def _pcm_stream():
                        yield first_chunk
                        yield from chunk_iter
                        _log_openai_success(streamed=True)

                    return StreamingResponse(
                        _pcm_stream(),
                        media_type="audio/L16;codec=pcm;rate=24000",
                        headers={"X-TTS-Mode": "openai", "X-TTS-Voice": ov},
                    )
                except Exception as exc:
                    logger.error(f"OpenAI TTS stream error: {exc}")
                    _metrics.record_error(
                        "web_tts_error",
                        labels={"mode": "openai", "error_type": "openai_tts_stream"},
                    )
                    # fall through to unary attempt + Cloud fallback below

            try:
                _track_tts(len(text))
                pcm_data = await asyncio.get_event_loop().run_in_executor(
                    None, _synthesize_openai_tts_pcm, text, ov
                )
                _log_openai_success(streamed=False)
                return Response(
                    content=pcm_data,
                    media_type="audio/L16;codec=pcm;rate=24000",
                    headers={"X-TTS-Mode": "openai", "X-TTS-Voice": ov},
                )
            except Exception as exc:
                logger.error(f"OpenAI TTS error: {exc}")
                _metrics.record_error(
                    "web_tts_error",
                    labels={"mode": "openai", "error_type": "openai_tts"},
                )
                # Last resort: native Cloud TTS voice so the assistant is
                # never mute just because OpenAI is down or rate-limited.
                fallback_voice = _tts_fallback_voice()
                if fallback_voice:
                    try:
                        pcm_data = await asyncio.get_event_loop().run_in_executor(
                            None, _synthesize_cloud_tts_pcm, text, fallback_voice
                        )
                        logger.warning(
                            "OpenAI TTS failed; served Cloud TTS fallback voice %s",
                            fallback_voice,
                        )
                        return Response(
                            content=pcm_data,
                            media_type="audio/L16;codec=pcm;rate=24000",
                            headers={
                                "X-TTS-Mode": "openai-fallback-cloud",
                                "X-TTS-Voice": fallback_voice,
                            },
                        )
                    except Exception as fb_exc:
                        logger.error(f"Cloud TTS fallback also failed: {fb_exc}")
                raise HTTPException(status_code=502, detail=f"OpenAI TTS failed: {exc}")

        # Native Cloud TTS (Chirp3-HD) path — selected by a full voice id like
        # "hr-HR-Chirp3-HD-Charon". Always unary (returns full PCM16 @ 24 kHz).
        if _is_cloud_tts_voice(voice_name):
            try:
                _track_tts(len(text))
                pcm_data = await asyncio.get_event_loop().run_in_executor(
                    None, _synthesize_cloud_tts_pcm, text, voice_name
                )
                duration_s = _time.time() - started_at
                _metrics.record_timing(
                    "web_tts_latency",
                    duration_s,
                    labels={"mode": "cloud", "vertex": "cloud_tts"},
                )
                _metrics.log_event(
                    "web_tts_request",
                    {
                        "voice_name": voice_name,
                        "model": "cloud-tts-chirp3hd",
                        "stream": False,
                        "vertex": False,
                        "text_chars": len(text),
                        "duration_ms": int(duration_s * 1000),
                        "success": True,
                    },
                )
                return Response(
                    content=pcm_data,
                    media_type="audio/L16;codec=pcm;rate=24000",
                    headers={"X-TTS-Mode": "cloud", "X-TTS-Voice": voice_name},
                )
            except Exception as exc:
                logger.error(f"Cloud TTS error: {exc}")
                _metrics.record_error(
                    "web_tts_error",
                    labels={"mode": "cloud", "error_type": "cloud_tts"},
                )
                raise HTTPException(status_code=502, detail=f"Cloud TTS failed: {exc}")

        try:
            _ = _create_tts_client()
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))

        try:
            _track_tts(len(text))

            if stream_audio:
                def stream_generator():
                    stream_started_at = _time.time()
                    try:
                        yield from _iter_tts_stream_chunks(text, voice_name, model_name)
                        duration_s = _time.time() - stream_started_at
                        _metrics.record_timing(
                            "web_tts_latency",
                            duration_s,
                            labels={"mode": "stream", "vertex": str(_tts_use_vertex())},
                        )
                        _metrics.log_event(
                            "web_tts_request",
                            {
                                "voice_name": voice_name,
                                "model": model_name,
                                "stream": True,
                                "vertex": _tts_use_vertex(),
                                "text_chars": len(text),
                                "duration_ms": int(duration_s * 1000),
                                "success": True,
                            },
                        )
                    except Exception as exc:
                        logger.error(f"TTS streaming error: {exc}")
                        duration_s = _time.time() - stream_started_at
                        error_type, _, is_quota = classify_google_runtime_error(exc)
                        if is_quota:
                            _track_quota_error("tts")
                        _metrics.record_error(
                            "web_tts_error",
                            labels={"mode": "stream", "error_type": error_type},
                        )
                        _metrics.log_event(
                            "web_tts_request",
                            {
                                "voice_name": voice_name,
                                "model": model_name,
                                "stream": True,
                                "vertex": _tts_use_vertex(),
                                "text_chars": len(text),
                                "duration_ms": int(duration_s * 1000),
                                "success": False,
                                "error_type": error_type,
                                "quota_error": is_quota,
                                "error": str(exc)[:300],
                            },
                        )
                        raise

                return StreamingResponse(
                    stream_generator(),
                    media_type="audio/L16;codec=pcm;rate=24000",
                    headers={"X-TTS-Mode": "stream"},
                )

            def synthesize_once():
                client = _create_tts_client()
                config = _build_tts_config(voice_name)
                response = client.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=config,
                )
                return _extract_pcm_from_tts_response(response)

            async def synthesize_with_retry():
                return await asyncio.get_event_loop().run_in_executor(None, synthesize_once)

            pcm_data = await run_with_bounded_retry(
                "tts_unary",
                synthesize_with_retry,
                config=RetryConfig(max_retries=1, base_delay=1.0, max_delay=4.0),
                log=logger,
            )
            duration_s = _time.time() - started_at
            _metrics.record_timing(
                "web_tts_latency",
                duration_s,
                labels={"mode": "unary", "vertex": str(_tts_use_vertex())},
            )
            _metrics.log_event(
                "web_tts_request",
                {
                    "voice_name": voice_name,
                    "model": model_name,
                    "stream": False,
                    "vertex": _tts_use_vertex(),
                    "text_chars": len(text),
                    "duration_ms": int(duration_s * 1000),
                    "success": True,
                },
            )
            return Response(
                content=pcm_data,
                media_type="audio/L16;codec=pcm;rate=24000",
                headers={"X-TTS-Mode": "unary"},
            )
        except Exception as e:
            logger.error(f"TTS error: {e}")
            duration_s = _time.time() - started_at
            error_text = str(e)
            error_type, _, is_quota = classify_google_runtime_error(e)
            if is_quota:
                _track_quota_error("tts")
            _metrics.record_error(
                "web_tts_error",
                labels={"mode": "stream" if stream_audio else "unary", "error_type": error_type},
            )
            _metrics.log_event(
                "web_tts_request",
                {
                    "voice_name": voice_name,
                    "model": model_name,
                    "stream": stream_audio,
                    "vertex": _tts_use_vertex(),
                    "text_chars": len(text),
                    "duration_ms": int(duration_s * 1000),
                    "success": False,
                    "error_type": error_type,
                    "quota_error": is_quota,
                    "error": error_text[:300],
                },
            )
            raise HTTPException(status_code=500, detail=str(e))

    # --- Gemini Live Voice WebSocket ---
    @app.websocket("/api/live")
    async def live_voice(websocket: WebSocket):
        """Real-time voice via Gemini 3.1 Flash Live API (PCM16 audio streaming)."""
        # Token auth via query param (WebSocket doesn't support custom headers in browsers)
        api_token = os.environ.get("API_TOKEN", "")
        if api_token:
            token = websocket.query_params.get("token", "")
            if token != api_token:
                await websocket.close(code=4001)
                return

        api_key = os.environ.get("GEMINI_API_KEY", "")

        await websocket.accept()

        if not api_key:
            logger.error("GEMINI_API_KEY not set — Live voice unavailable")
            await websocket.send_json({"type": "error", "message": "GEMINI_API_KEY nije postavljen na serveru"})
            await websocket.close(code=4002)
            return
        logger.info("Live voice session started")

        try:
            from google import genai
            from google.genai import types
            from google.genai.types import (
                LiveConnectConfig, SpeechConfig, VoiceConfig,
                PrebuiltVoiceConfig, Content, Part
            )

            # Gemini Live API requires Gemini API endpoint (not Vertex AI).
            # Temporarily remove Vertex AI env vars for client creation only.
            # Safe in single-threaded asyncio — no await between pop and restore.
            _vertex_keys = genai_vertex_env_keys()
            _vertex_backup = {k: os.environ.pop(k) for k in _vertex_keys if k in os.environ}
            try:
                client = genai.Client(api_key=api_key)
            finally:
                os.environ.update(_vertex_backup)

            config = LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=Content(parts=[Part(text=_build_live_system_prompt())]),
                speech_config=SpeechConfig(
                    voice_config=VoiceConfig(
                        prebuilt_voice_config=PrebuiltVoiceConfig(
                            voice_name=os.environ.get("TTS_VOICE_NAME", _default_tts_voice_name())
                        )
                    )
                )
            )

            async with client.aio.live.connect(
                model=os.environ.get("LIVE_VOICE_MODEL", "gemini-3.1-flash-live-preview"),
                config=config
            ) as session:

                async def send_audio():
                    chunks_sent = 0
                    try:
                        while True:
                            data = await websocket.receive_bytes()
                            await session.send_realtime_input(
                                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                            )
                            chunks_sent += 1
                            if chunks_sent % 50 == 0:
                                logger.debug(f"Live: sent {chunks_sent} audio chunks")
                    except Exception as e:
                        logger.info(f"Live send_audio ended ({chunks_sent} chunks sent): {type(e).__name__}: {e}")

                async def receive_audio():
                    # PCM16 @ 24kHz = 48000 bytes/sec
                    # Buffer min 100ms (4800 bytes) to avoid choppy 2-byte micro-chunks
                    MIN_SEND_BYTES = 4800
                    audio_buf = bytearray()
                    responses_received = 0
                    audio_bytes_total = 0

                    async def _flush(force: bool = False):
                        nonlocal audio_buf
                        if not audio_buf:
                            return
                        if force or len(audio_buf) >= MIN_SEND_BYTES:
                            await websocket.send_bytes(bytes(audio_buf))
                            audio_buf = bytearray()

                    try:
                        # Outer while loop: re-enter session.receive() after each
                        # turn_complete so multi-turn conversation works.
                        while True:
                            turn_done = False
                            async for response in session.receive():
                                responses_received += 1

                                raw: bytes | None = None
                                if response.data:
                                    raw = response.data

                                sc = getattr(response, 'server_content', None)
                                if sc:
                                    mt = getattr(sc, 'model_turn', None)
                                    if mt:
                                        for part in getattr(mt, 'parts', []):
                                            inline = getattr(part, 'inline_data', None)
                                            if inline and getattr(inline, 'data', None):
                                                raw = inline.data
                                            txt = getattr(part, 'text', None)
                                            if txt:
                                                await websocket.send_json({"type": "transcript", "text": txt})
                                    ot = getattr(sc, 'output_transcription', None)
                                    if ot and getattr(ot, 'text', None):
                                        await websocket.send_json({"type": "transcript", "text": ot.text})
                                    if getattr(sc, 'turn_complete', False):
                                        if raw:
                                            audio_buf.extend(raw)
                                            audio_bytes_total += len(raw)
                                            raw = None
                                        await _flush(force=True)
                                        turn_done = True
                                        continue

                                txt = getattr(response, 'text', None)
                                if txt:
                                    await websocket.send_json({"type": "transcript", "text": txt})

                                if raw:
                                    audio_buf.extend(raw)
                                    audio_bytes_total += len(raw)
                                    await _flush()

                            # session.receive() generator exhausted — either turn_complete
                            # fired (keep going) or session truly closed (break).
                            if not turn_done:
                                break  # session closed by Gemini, stop looping
                            # else: turn finished normally, loop back to receive next turn

                    except Exception as e:
                        logger.error(f"Live receive error ({responses_received} resp, {audio_bytes_total}B): {type(e).__name__}: {e}")
                    finally:
                        await _flush(force=True)
                        logger.info(f"Live receive_audio done: {responses_received} responses, {audio_bytes_total} audio bytes")

                await asyncio.gather(send_audio(), receive_audio())

        except Exception as e:
            logger.error(f"Live voice session error: {e}")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
            logger.info("Live voice session ended")

    # --- Session endpoints ---
    @app.get("/api/sessions", response_model=List[SessionInfo])
    async def list_sessions():
        return _web_interface.list_sessions()

    @app.post("/api/sessions/new")
    async def new_session(user_id: str = "web-user"):
        session_id = _web_interface.create_new_session(user_id)
        return {"session_id": session_id, "user_id": user_id}

    @app.post("/api/sessions/{session_id}/switch")
    async def switch_session(session_id: str, user_id: str = "web-user"):
        success = _web_interface.switch_session(user_id, session_id)
        return {"success": success, "session_id": session_id}

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str):
        return _web_interface.get_history(session_id)

    # --- Agents endpoint ---
    @app.get("/api/agents", response_model=List[AgentInfo])
    async def list_agents():
        agents = []
        for name in get_worker_agent_names():
            config = get_agent_config(name)
            if config:
                agents.append(AgentInfo(
                    name=config.name,
                    model=config.model,
                    description=config.description,
                    tools=config.tools
                ))
        return agents

    # --- Status endpoint ---
    @app.get("/api/status")
    async def system_status():
        base_status = _web_interface.get_status()

        # Rate limiter metrics
        rate_data = {}
        services_data = {}
        try:
            from tools.resilience.rate_limiter import (
                get_all_metrics, get_service_status, SERVICE_CONFIGS
            )
            rate_data = get_all_metrics()
            for svc in SERVICE_CONFIGS:
                try:
                    services_data[svc] = get_service_status(svc)
                except Exception:
                    services_data[svc] = {"status": "unavailable"}
        except ImportError:
            pass

        return {
            **base_status,
            "deployment": deployment_config.to_public_dict(),
            "rate_limiter": rate_data,
            "services": services_data
        }

    # --- Cost & Quota monitoring ---
    @app.get("/api/costs")
    async def get_costs():
        uptime_h = (_time.time() - _cost_tracker["start_time"]) / 3600

        # LLM breakdown po modelu
        llm_breakdown = []
        for key in _cost_tracker["llm_calls"]:
            llm_breakdown.append({
                "model": key,
                "calls": _cost_tracker["llm_calls"][key],
                "input_tokens": _cost_tracker["llm_input_tokens"][key],
                "output_tokens": _cost_tracker["llm_output_tokens"][key],
                "cost_usd": round(_cost_tracker["llm_cost_usd"][key], 4),
            })
        llm_breakdown.sort(key=lambda x: x["cost_usd"], reverse=True)

        total_llm = sum(_cost_tracker["llm_cost_usd"].values())
        total_cost = total_llm + _cost_tracker["veo_cost_usd"] + _cost_tracker["imagen_cost_usd"]

        # 429 info
        quota_errors = dict(_cost_tracker["quota_errors"])
        last_errors = {}
        for svc, ts in _cost_tracker["quota_errors_last"].items():
            mins_ago = (_time.time() - ts) / 60
            last_errors[svc] = f"{mins_ago:.0f}m ago" if mins_ago < 60 else f"{mins_ago/60:.1f}h ago"

        return {
            "uptime_hours": round(uptime_h, 2),
            "total_cost_usd": round(total_cost, 4),
            "total_cost_eur": round(total_cost * 0.92, 4),
            "llm": {
                "total_usd": round(total_llm, 4),
                "breakdown": llm_breakdown,
            },
            "tts": {
                "calls": _cost_tracker["tts_calls"],
                "chars": _cost_tracker["tts_chars"],
                "cost_usd": round(_cost_tracker["tts_cost_usd"], 5),
                "note": "preview - trenutno besplatno",
            },
            "veo": {
                "calls": _cost_tracker["veo_calls"],
                "cost_usd": round(_cost_tracker["veo_cost_usd"], 2),
                "cost_per_video": 0.40,
            },
            "imagen": {
                "calls": _cost_tracker["imagen_calls"],
                "cost_usd": round(_cost_tracker["imagen_cost_usd"], 3),
                "cost_per_image": 0.04,
            },
            "quota_errors": quota_errors,
            "quota_errors_last": last_errors,
            "total_quota_errors": sum(quota_errors.values()),
        }

    @app.get("/api/monitoring/summary")
    async def monitoring_summary(limit: int = 40):
        return {
            "metrics": _metrics.get_metrics(),
            "recent_events": _metrics.get_recent_events(limit=limit),
        }

    # --- Trace endpoint ---
    @app.get("/api/trace/{session_id}")
    async def get_trace(session_id: str):
        return _web_interface.get_trace(session_id)

    # --- Media endpoints ---
    @app.post("/api/upload")
    async def upload_file(
        file: UploadFile = File(...),
        user_id: str = Form(default="web-user"),
        session_id: str = Form(default=""),
    ):
        """
        Upload an image/PDF file. Returns file_id and base64 for agent processing.
        The frontend sends the file, then includes the file_id in the chat message.
        """
        from services.media_service import (
            save_upload, sniff_mime, ALLOWED_MIME_TYPES, MAX_FILE_SIZE
        )

        # Validate the declared MIME type first — it is free to check and
        # rejects the honest mistakes before any bytes are read.
        if file.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {file.content_type}. "
                       f"Allowed: {', '.join(ALLOWED_MIME_TYPES)}"
            )

        # Read in chunks and stop at the limit. Reading the whole upload first
        # and checking its length afterwards means the machine has already paid
        # for it: on a Pi, one oversized upload is enough to exhaust memory
        # before the check that was supposed to prevent exactly that.
        chunks = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                logger.warning(
                    "Upload refused: exceeded %d bytes (declared %s)",
                    MAX_FILE_SIZE, file.content_type,
                )
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Max: {MAX_FILE_SIZE} bytes",
                )
            chunks.append(chunk)
        file_bytes = b"".join(chunks)

        # And check what the bytes actually are. content_type is supplied by the
        # client, so on its own it establishes nothing.
        actual = sniff_mime(file_bytes[:16])
        if actual is None or actual not in ALLOWED_MIME_TYPES:
            logger.warning(
                "Upload refused: content does not match any allowed format "
                "(declared %s, detected %s)", file.content_type, actual,
            )
            raise HTTPException(
                status_code=400,
                detail="File content does not match an allowed format.",
            )

        # Save locally
        result = save_upload(file_bytes, file.filename or "upload", file.content_type)

        # Return file_id + base64 for immediate use
        b64_data = base64.b64encode(file_bytes).decode("utf-8")

        return {
            "file_id": result["file_id"],
            "filename": result["original_name"],
            "mime_type": result["mime_type"],
            "size": result["size"],
            "base64": b64_data,
            "url": f"/api/media/{result['file_id']}",
        }

    @app.get("/api/media/{file_id}")
    async def serve_media(file_id: str):
        """Serve an uploaded or generated media file."""
        from services.media_service import get_file_path

        path = get_file_path(file_id)
        if not path or not os.path.exists(path):
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(path)

    # --- HITL (Human-in-the-Loop) approval endpoints ---

    @app.get("/api/hitl/pending")
    async def hitl_list_pending():
        """List all pending HITL approval requests."""
        from services.hitl_firestore_service import get_hitl_firestore_service
        return await get_hitl_firestore_service().list_pending()

    @app.get("/api/hitl/{confirmation_id}")
    async def hitl_get_one(confirmation_id: str):
        """Get a single HITL confirmation (any status)."""
        from services.hitl_firestore_service import get_hitl_firestore_service
        data = await get_hitl_firestore_service().get_one(confirmation_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Confirmation not found")
        return data

    @app.post("/api/hitl/{confirmation_id}/approve")
    async def hitl_approve(confirmation_id: str):
        """Approve a pending HITL confirmation."""
        from services.hitl_firestore_service import get_hitl_firestore_service
        ok = await get_hitl_firestore_service().approve(confirmation_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Confirmation not found or already decided")
        return {"confirmation_id": confirmation_id, "status": "approved"}

    @app.post("/api/hitl/{confirmation_id}/reject")
    async def hitl_reject(confirmation_id: str, reason: str = ""):
        """Reject a pending HITL confirmation."""
        from services.hitl_firestore_service import get_hitl_firestore_service
        ok = await get_hitl_firestore_service().reject(confirmation_id, reason=reason)
        if not ok:
            raise HTTPException(status_code=404, detail="Confirmation not found or already decided")
        return {"confirmation_id": confirmation_id, "status": "rejected", "reason": reason}

    return app
