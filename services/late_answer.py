"""
Deliver an answer that outlived the window it was asked in.

Home Assistant Assist on a phone is a conversation window, not a mailbox: it
dies when the screen sleeps or the app goes to the background, and the pipeline
run dies with it. Measured on this system 2026-09-03: a 53 s turn came back
complete and was spoken, while a 3 min 25 s research task was not — the agent
finished it (POST /api/chat answered 200) and there was nobody left to speak
to.

So a slow answer stops chasing that window. The turn says out loud that the
work is running, and the finished answer arrives on the phone as a Home
Assistant notification, which survives a locked screen.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def notify_service() -> str:
    """The HA notify service that reaches the phone, as `domain.service`."""
    return os.getenv("HA_NOTIFY_SERVICE", "notify.notify").strip()


def _max_chars() -> int:
    try:
        return max(120, int(os.getenv("LATE_ANSWER_MAX_CHARS", "2000")))
    except ValueError:
        return 2000


def _speaks() -> bool:
    return os.getenv("LATE_ANSWER_MODE", "notify").strip().lower() == "speak"


def build_payload(question: str, answer: str) -> dict:
    """What gets POSTed to the notify service."""
    text = (answer or "").strip()
    limit = _max_chars()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"

    if _speaks():
        # The Android companion app reads `tts_text` out loud; the title and
        # body are ignored in this mode, so the question is not repeated.
        return {
            "message": "TTS",
            "data": {
                "tts_text": text,
                "media_stream": os.getenv("LATE_ANSWER_STREAM", "music_stream"),
            },
        }

    asked = " ".join((question or "").split())
    if len(asked) > 60:
        asked = asked[:59].rstrip() + "…"
    return {
        "title": f"Jarvis: {asked}" if asked else "Jarvis",
        "message": text,
    }


def deliver_sync(question: str, answer: str) -> bool:
    """Send one finished answer to the phone. Never raises."""
    service = notify_service()
    if not service or "." not in service:
        logger.warning("HA_NOTIFY_SERVICE is not a domain.service value: %r", service)
        return False

    domain, _, name = service.partition(".")
    try:
        from tools.adk_tools.ha_adk_tools import _ha_request

        _ha_request(f"/api/services/{domain}/{name}", build_payload(question, answer))
        logger.info("Late answer delivered via %s (%d chars)", service, len(answer or ""))
        return True
    except Exception as err:  # noqa: BLE001 - a failed delivery must not raise into a callback
        logger.warning("Late answer could not be delivered via %s: %s", service, err)
        return False


async def deliver(question: str, answer: str) -> bool:
    return await asyncio.get_event_loop().run_in_executor(
        None, deliver_sync, question, answer
    )


def deliver_when_done(task: "asyncio.Future[str]", question: str) -> None:
    """Attach delivery to a run that is still going.

    The task is deliberately not awaited anywhere: the caller has already
    answered the user and returned, and the work must survive that.
    """
    loop = asyncio.get_event_loop()

    def _finished(done: "asyncio.Future[str]") -> None:
        if done.cancelled():
            logger.info("Deferred voice task was cancelled; nothing to deliver")
            return
        error = done.exception()
        if error is not None:
            logger.warning("Deferred voice task failed: %s", error)
            answer: Optional[str] = f"Zadatak nije uspio: {error}"
        else:
            answer = done.result()
        if not (answer or "").strip():
            return
        loop.create_task(deliver(question, answer))

    task.add_done_callback(_finished)
