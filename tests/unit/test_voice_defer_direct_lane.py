"""The fast voice lanes are not always fast, and silence is the worst answer.

Assist on the phone stops listening well before a slow turn finishes. The
orchestrator path knew that -- past a grace period it starts a background job,
promises an answer, and services/late_answer puts the result on the phone. The
DIRECT lanes (smart_home, secretary, voice_qa...) had none of that, on the
assumption that skipping the orchestrator made them quick.

Measured 2026-09-06: "pusti Arena Sport 1" ran smart_home directly and took 40
seconds, 23.6 of them waiting for the A1 Xplore app to load. Assist had given
up, and since no job was ever created there was no notification either. The
channel had actually changed. The user heard nothing and reported it as broken.

Run with:
    pytest tests/unit/test_voice_defer_direct_lane.py -v
"""

import asyncio
from types import SimpleNamespace

import pytest

from interfaces.base_interface import HA_ASSIST_USER_PREFIX, BaseInterface


@pytest.fixture
def iface():
    class _Iface(BaseInterface):
        def __init__(self):
            super().__init__(session_prefix="test")
            self.system = SimpleNamespace(philosophy_keywords=set())

        async def start(self):  # pragma: no cover
            pass

        async def stop(self):  # pragma: no cover
            pass

        def format_response(self, response):  # pragma: no cover
            return response

    return _Iface()


@pytest.fixture
def ctx():
    return SimpleNamespace(
        session_id="s-1", user_id="ha-assist-1", helper=SimpleNamespace()
    )


@pytest.fixture(autouse=True)
def quick_grace(monkeypatch):
    monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.2")


@pytest.fixture
def promises(monkeypatch):
    """Capture what would have been sent to the phone."""
    delivered = []
    monkeypatch.setattr(
        "services.late_answer.deliver_when_done",
        lambda task, question: delivered.append(question),
    )
    monkeypatch.setattr(
        "services.background_jobs.start", lambda s, u, q: "job-1"
    )
    return delivered


ASSIST_USER = HA_ASSIST_USER_PREFIX + "1"


class TestASlowTurnIsHandedToThePhone:
    @pytest.mark.asyncio
    async def test_the_caller_gets_the_promise_not_the_silence(
        self, iface, ctx, promises
    ):
        async def slow():
            await asyncio.sleep(5)
            return "kanal prebacen"

        answer = await iface._defer_to_phone_if_slow(
            slow, user_id=ASSIST_USER, question="pusti Arena Sport 1", ctx=ctx
        )
        assert "mobitel" in answer.lower()
        assert promises == ["pusti Arena Sport 1"]

    @pytest.mark.asyncio
    async def test_the_work_is_not_cancelled_by_the_timeout(
        self, iface, ctx, promises
    ):
        """The grace period ends the waiting, never the work."""
        finished = asyncio.Event()

        async def slow():
            await asyncio.sleep(0.4)
            finished.set()
            return "gotovo"

        await iface._defer_to_phone_if_slow(
            slow, user_id=ASSIST_USER, question="q", ctx=ctx
        )
        await asyncio.wait_for(finished.wait(), timeout=2)


class TestAQuickTurnIsUnaffected:
    @pytest.mark.asyncio
    async def test_it_answers_normally(self, iface, ctx, promises):
        async def quick():
            return "upalio sam svjetlo"

        answer = await iface._defer_to_phone_if_slow(
            quick, user_id=ASSIST_USER, question="upali svjetlo", ctx=ctx
        )
        assert answer == "upalio sam svjetlo"
        assert promises == [], "nothing should have been promised to the phone"


class TestOnlyAssistDefers:
    @pytest.mark.asyncio
    async def test_the_wake_word_speaker_waits_it_out(self, iface, ctx, promises):
        """There is someone in the room; deferring would talk over them."""
        async def slow():
            await asyncio.sleep(0.4)
            return "gotovo"

        answer = await iface._defer_to_phone_if_slow(
            slow, user_id="rpi-voice-1", question="q", ctx=ctx
        )
        assert answer == "gotovo"
        assert promises == []

    @pytest.mark.asyncio
    async def test_a_zero_grace_disables_it(self, iface, ctx, promises, monkeypatch):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0")

        async def slow():
            await asyncio.sleep(0.3)
            return "gotovo"

        answer = await iface._defer_to_phone_if_slow(
            slow, user_id=ASSIST_USER, question="q", ctx=ctx
        )
        assert answer == "gotovo"
        assert promises == []

    @pytest.mark.asyncio
    async def test_the_coroutine_is_not_even_created_when_unused(self, iface, ctx):
        """`start` is a factory precisely so nothing is left un-awaited."""
        made = []

        def start():
            made.append(1)
            async def body():
                return "ok"
            return body()

        await iface._defer_to_phone_if_slow(
            start, user_id="rpi-voice-1", question="q", ctx=ctx
        )
        assert made == [1]
