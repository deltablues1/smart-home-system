"""Wyoming server entry point: Jarvis STT and TTS for Home Assistant.

Usage:
    python scripts/run_wyoming.py                  # tcp://0.0.0.0:10400
    python scripts/run_wyoming.py --uri tcp://0.0.0.0:10500

Home Assistant then adds it once under Settings -> Devices -> Wyoming Protocol,
and both the speech-to-text and the text-to-speech service appear.

Environment:
    WYOMING_URI        listen address (default tcp://0.0.0.0:10400)
    WYOMING_LANGUAGES  comma separated, default hr,en
    JARVIS_TTS_URL     default http://127.0.0.1:8000/api/tts
    API_TOKEN          sent to /api/tts when the web API requires it
"""

import argparse
import asyncio
import logging
import os
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("run_wyoming")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from utils.process_guard import hard_exit, notify_ready, notify_watchdog, watchdog_interval_seconds  # noqa: E402


async def _serve(uri: str) -> None:
    from wyoming.server import AsyncServer

    from services.wyoming_bridge import JarvisEventHandler, build_info

    languages = [
        lang.strip()
        for lang in os.getenv("WYOMING_LANGUAGES", "hr,en").split(",")
        if lang.strip()
    ]
    voice_name = os.getenv("OPENAI_TTS_VOICE", "cedar").strip() or "cedar"

    settings = {
        "tts_url": os.getenv("JARVIS_TTS_URL", "http://127.0.0.1:8000/api/tts"),
        "api_token": os.getenv("API_TOKEN", "").strip(),
        "tts_timeout": float(os.getenv("WYOMING_TTS_TIMEOUT_SECONDS", "60")),
    }

    info = build_info(languages, voice_name)
    logger.info("Serving Wyoming on %s", uri)
    logger.info("  STT: %s (%s)", os.getenv("STT_ENGINE", "gemini"), ",".join(languages))
    logger.info("  TTS: %s via %s", voice_name, settings["tts_url"])

    server = AsyncServer.from_uri(uri)

    # Tell systemd we are up, then keep proving it. Same reasoning as the other
    # services: a live PID is not evidence that the socket still serves.
    notify_ready("wyoming")
    heartbeat = watchdog_interval_seconds()
    if heartbeat:
        asyncio.create_task(_heartbeat(heartbeat))

    await server.run(partial(JarvisEventHandler, info, settings))


async def _heartbeat(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        notify_watchdog("wyoming")


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Wyoming STT/TTS bridge")
    parser.add_argument(
        "--uri",
        default=os.getenv("WYOMING_URI", "tcp://0.0.0.0:10400"),
        help="Listen address (default: WYOMING_URI env, else tcp://0.0.0.0:10400)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_serve(args.uri))
    except KeyboardInterrupt:
        logger.info("Shutdown complete")
        hard_exit(0, "interrupted by user")
    except Exception as err:  # noqa: BLE001
        logger.error("Fatal error: %s", err)
        import traceback

        traceback.print_exc()
        hard_exit(1, f"fatal error: {err}")
    else:
        logger.error("Wyoming server returned unexpectedly")
        hard_exit(1, "server returned unexpectedly")


if __name__ == "__main__":
    main()
