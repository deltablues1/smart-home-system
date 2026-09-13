"""A/B probe for the xAI Grok Voice API (no app changes needed).

Synthesizes a Croatian sentence with xAI TTS and saves an audio file you can
play, and optionally transcribes a Croatian audio file back with xAI STT, so
you can eyeball Croatian accuracy before considering STT_ENGINE/TTS_ENGINE=xai
in production. Croatian is NOT in xAI's officially listed languages (as of
2026-07), so this is purely a quality check, not a supported-language test.

Requires XAI_API_KEY in the environment (or .env).

Usage:
    # TTS -> audio file
    python scripts/xai_voice_probe.py tts [voice_id] [text]
    # STT: transcribe an existing audio file
    python scripts/xai_voice_probe.py stt <audio_path>
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

import requests

API_BASE = "https://api.x.ai/v1"
DEFAULT_TEXT = (
    "Dobar dan, ja sam Jarvis, vaš glasovni asistent. "
    "Drago mi je što mogu pomoći. Treba li još nešto?"
)


def _api_key() -> str:
    key = os.getenv("XAI_API_KEY", "").strip()
    if not key:
        sys.exit("XAI_API_KEY not set (env or .env)")
    return key


def run_tts(argv: list[str]) -> None:
    voice_id = argv[0] if len(argv) > 0 else os.getenv("XAI_TTS_VOICE", "eve")
    text = argv[1] if len(argv) > 1 else DEFAULT_TEXT

    resp = requests.post(
        f"{API_BASE}/tts",
        headers={"Authorization": f"Bearer {_api_key()}"},
        json={"text": text, "voice_id": voice_id, "language": "hr"},
        timeout=60,
    )
    if not resp.ok:
        sys.exit(f"TTS failed: {resp.status_code} {resp.text[:500]}")

    out = os.path.join(os.getenv("TEMP", "/tmp"), f"jarvis_xai_tts_{voice_id}.mp3")
    Path(out).write_bytes(resp.content)
    print(f"voice_id={voice_id} audio_bytes={len(resp.content)}")
    print(f"saved={out}")


def run_stt(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: xai_voice_probe.py stt <audio_path>")
    path = argv[0]

    with open(path, "rb") as f:
        resp = requests.post(
            f"{API_BASE}/stt",
            headers={"Authorization": f"Bearer {_api_key()}"},
            files={"file": f},
            timeout=60,
        )
    if not resp.ok:
        sys.exit(f"STT failed: {resp.status_code} {resp.text[:500]}")

    data = resp.json()
    print(f"transcript={(data.get('text') or '').strip()}")


if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "tts"
    rest = sys.argv[2:]
    if mode == "tts":
        run_tts(rest)
    elif mode == "stt":
        run_stt(rest)
    else:
        sys.exit("unknown mode %r (use 'tts' or 'stt')" % mode)
