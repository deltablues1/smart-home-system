"""A research question must not freeze the house.

google_search_grounding is an async function that called
client.models.generate_content -- the genai SDK's SYNCHRONOUS surface -- with no
await and no thread. It therefore held the one event loop for the whole request.

Measured 2026-09-06 on the Pi, with a voice research job running: three
grounding calls back to back (17:15:10-17:16:22, ->17:17:49, ->17:18:35) and the
web process answered nothing in between. "Upali svjetlo u hodniku" arrived at
17:16:10; the MQTT publish went out at 17:18:41, the instant the last grounding
call returned. The light sat off for two and a half minutes.

This test does not check for a keyword in the source -- it runs the coroutine
against a client whose generate_content sleeps, and watches whether anything
else on the loop still gets to run.

Run with:
    pytest tests/unit/test_grounding_does_not_block.py -v
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from tools.api_implementations import google_search_api

BLOCKING_SECONDS = 0.4


class _SleepyModels:
    """Stands in for the SDK's sync surface: it blocks the calling thread."""

    def generate_content(self, **kwargs):
        time.sleep(BLOCKING_SECONDS)
        return SimpleNamespace(text="odgovor", candidates=[])


@pytest.fixture
def sleepy_client(monkeypatch):
    monkeypatch.setattr(
        google_search_api.genai,
        "Client",
        lambda **kwargs: SimpleNamespace(models=_SleepyModels()),
    )
    monkeypatch.setattr(
        "tools.google_api_client.get_vertex_ai_config",
        lambda: {"project_id": "test-project"},
    )


class TestTheLoopKeepsBreathing:
    @pytest.mark.asyncio
    async def test_other_tasks_run_during_a_grounding_call(self, sleepy_client):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(ticker())
        try:
            await google_search_api.google_search_grounding(None, "test upit")
        finally:
            beat.cancel()

        # Bare, the sleep would pin the loop and ticks would stay at 0.
        assert ticks >= 5, (
            f"only {ticks} ticks during a {BLOCKING_SECONDS}s call -- the event "
            "loop was blocked, so nothing else in the process could answer"
        )

    @pytest.mark.asyncio
    async def test_a_light_switch_still_gets_through(self, sleepy_client):
        """The concrete failure: a fast action queued behind a slow search."""
        switched_at = None

        async def switch_the_light():
            nonlocal switched_at
            await asyncio.sleep(0.05)
            switched_at = time.monotonic()

        started = time.monotonic()
        _, _ = await asyncio.gather(
            google_search_api.google_search_grounding(None, "test upit"),
            switch_the_light(),
        )
        assert switched_at is not None
        assert switched_at - started < BLOCKING_SECONDS, (
            "the light waited for the search to finish"
        )
