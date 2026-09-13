#!/usr/bin/env python3
"""
RPi smoke check for the smart-home voice stack.

Checks only the high-signal preconditions before the first live test:
  - env/profile looks sane for rpi-home
  - wakeword/audio deps import
  - MQTT broker TCP reachability
  - local web API responds
  - local TTS endpoint returns audio

Optional chat/MQTT action execution is intentionally omitted here to keep the
script safe to run before a real household test.
"""

import os
import sys
import json
import socket
from pathlib import Path

import requests
from dotenv import load_dotenv


def ok(msg: str):
    print(f"[OK] {msg}")


def warn(msg: str):
    print(f"[WARN] {msg}")


def fail(msg: str):
    print(f"[FAIL] {msg}")


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    failures = 0

    profile = os.getenv("DEPLOYMENT_PROFILE", "")
    if profile == "rpi-home":
        ok("DEPLOYMENT_PROFILE=rpi-home")
    else:
        fail(f"DEPLOYMENT_PROFILE is {profile!r}, expected 'rpi-home'")
        failures += 1

    if os.getenv("WAKE_WORD_ENABLED", os.getenv("ENABLE_WAKE_WORD", "")).lower() in ("1", "true", "yes", "on"):
        ok("ENABLE_WAKE_WORD is enabled")
    else:
        warn("ENABLE_WAKE_WORD is not enabled")

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        ok("GEMINI_API_KEY is present")
    else:
        fail("GEMINI_API_KEY is missing")
        failures += 1

    token_path = Path(
        os.getenv("OAUTH_TOKEN_STORAGE_PATH", str(Path.home() / ".google_workspace_adk" / "tokens.json"))
    )
    if token_path.exists():
        ok(f"OAuth token file exists: {token_path}")
    else:
        warn(f"OAuth token file not found: {token_path}")

    # Wakeword deps
    try:
        import pyaudio  # noqa: F401
        import webrtcvad  # noqa: F401
        import openwakeword  # noqa: F401
        ok("wakeword/audio Python deps import successfully")
    except Exception as exc:
        fail(f"wakeword/audio deps import failed: {exc}")
        failures += 1

    # MQTT broker TCP reachability
    broker = os.getenv("MQTT_BROKER", "homeassistant.local").strip()
    port = int(os.getenv("MQTT_PORT", "1883"))
    try:
        with socket.create_connection((broker, port), timeout=3):
            ok(f"MQTT broker reachable at {broker}:{port}")
    except Exception as exc:
        fail(f"MQTT broker not reachable at {broker}:{port}: {exc}")
        failures += 1

    api_url = os.getenv("WAKEWORD_API_URL", "http://localhost:8000").rstrip("/")
    headers = {}
    api_token = os.getenv("API_TOKEN", "").strip()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    # Web status endpoint
    try:
        resp = requests.get(f"{api_url}/api/status", headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        ok(f"Web API reachable: {api_url}/api/status ({data.get('status', 'ok')})")
    except Exception as exc:
        fail(f"Web API status check failed: {exc}")
        failures += 1

    # TTS endpoint
    try:
        resp = requests.post(
            f"{api_url}/api/tts",
            headers=headers,
            json={"text": "Ovo je kratki test govora za Raspberry Pi."},
            timeout=20,
        )
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError("empty audio body")
        ok(f"TTS endpoint returned {len(resp.content)} bytes of audio")
    except Exception as exc:
        fail(f"TTS check failed: {exc}")
        failures += 1

    print()
    if failures:
        fail(f"RPi smoke check failed with {failures} blocking issue(s)")
        return 1

    ok("RPi smoke check passed")
    print("Next: start adk-web + adk-wakeword and say 'hey jarvis'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
