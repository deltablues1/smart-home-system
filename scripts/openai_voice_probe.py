"""A/B probe for the optional OpenAI voice engine (no app changes needed).

Synthesizes a Croatian sentence with OpenAI TTS and saves a WAV you can play,
and optionally transcribes an audio file back with OpenAI STT to eyeball
Croatian accuracy before flipping STT_ENGINE/TTS_ENGINE in production.

Requires OPENAI_API_KEY in the environment (or .env).

Usage:
    # TTS -> WAV
    python scripts/openai_voice_probe.py tts [voice] [text]
    # STT: transcribe an existing audio file
    python scripts/openai_voice_probe.py stt <audio_path> [lang]
"""
import io
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI

DEFAULT_TEXT = (
    "Dobar dan, ja sam Jarvis, vaš glasovni asistent. "
    "Drago mi je što mogu pomoći. Treba li još nešto?"
)


def _client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY not set (env or .env)")
    return OpenAI(api_key=key)


def run_tts(argv: list[str]) -> None:
    voice = argv[0] if len(argv) > 0 else os.getenv("OPENAI_TTS_VOICE", "alloy")
    text = argv[1] if len(argv) > 1 else DEFAULT_TEXT
    model = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

    resp = _client().audio.speech.create(
        model=model, voice=voice, input=text, response_format="pcm"
    )
    pcm = resp.read() if hasattr(resp, "read") else resp.content

    # Wrap raw PCM16 @ 24 kHz mono into a WAV so it is playable everywhere.
    out = os.path.join(
        os.getenv("TEMP", "/tmp"), "jarvis_openai_tts_%s.wav" % voice
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    Path(out).write_bytes(buf.getvalue())
    print("model=%s voice=%s pcm_bytes=%d" % (model, voice, len(pcm)))
    print("saved=%s" % out)


def run_stt(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: openai_voice_probe.py stt <audio_path> [lang]")
    path = argv[0]
    lang = argv[1] if len(argv) > 1 else os.getenv("OPENAI_STT_LANGUAGE", "hr")
    model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")

    with open(path, "rb") as f:
        resp = _client().audio.transcriptions.create(
            model=model, file=f, language=lang
        )
    print("model=%s lang=%s" % (model, lang))
    print("transcript=%s" % (getattr(resp, "text", "") or "").strip())


if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "tts"
    rest = sys.argv[2:]
    if mode == "tts":
        run_tts(rest)
    elif mode == "stt":
        run_stt(rest)
    else:
        sys.exit("unknown mode %r (use 'tts' or 'stt')" % mode)
