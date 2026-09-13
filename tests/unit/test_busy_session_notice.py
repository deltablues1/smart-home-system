"""A busy session must say so, not go silent.

Measured 2026-09-06 on the Pi: while a 15-minute research run held a session,
"Je li gotovo?" sent to that same session waited on the lock for four and a half
minutes and came back with nothing at all -- the client timed out first. The
serialization is correct (one turn per session at a time); the silence is not.
On the voice lane especially, silence is indistinguishable from a crash.

A second message now waits only a grace period, then gets told what is running,
for how long, and that its own message was NOT taken.

Run with:
    pytest tests/unit/test_busy_session_notice.py -v
"""

import asyncio
import time

import pytest

from interfaces.base_interface import turn_busy_notice_after_seconds
from interfaces.web_interface import WebInterface

GRACE = 0.2


@pytest.fixture(autouse=True)
def short_grace(monkeypatch):
    monkeypatch.setenv("TURN_BUSY_NOTICE_AFTER_SECONDS", str(GRACE))


@pytest.fixture
def iface(monkeypatch):
    """A WebInterface with only the turn-serialization parts wired up."""
    obj = WebInterface.__new__(WebInterface)
    obj.processing_locks = {}
    obj._inflight_turns = {}
    monkeypatch.setattr(WebInterface, "switch_session", lambda self, u, s: True)
    monkeypatch.setattr(WebInterface, "get_or_create_session", lambda self, u: "s-1")
    return obj


class TestTheKnob:
    def test_default_is_ten_seconds(self, monkeypatch):
        monkeypatch.delenv("TURN_BUSY_NOTICE_AFTER_SECONDS", raising=False)
        assert turn_busy_notice_after_seconds() == 10.0

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("TURN_BUSY_NOTICE_AFTER_SECONDS", "soon")
        assert turn_busy_notice_after_seconds() == 10.0

    def test_it_never_goes_to_zero(self, monkeypatch):
        """Zero would mean two quick messages could never queue at all."""
        monkeypatch.setenv("TURN_BUSY_NOTICE_AFTER_SECONDS", "0")
        assert turn_busy_notice_after_seconds() >= 1.0


class TestTheNotice:
    def test_it_names_the_running_request_and_the_elapsed_time(self, iface):
        iface._inflight_turns["s-1"] = {
            "started_at": time.time() - 125,
            "message": "Istrazi prednosti toplinskih pumpi",
        }
        notice = iface.busy_notice("s-1")
        assert "toplinskih pumpi" in notice
        assert "2 min" in notice

    def test_it_says_this_message_was_not_taken(self, iface):
        iface._inflight_turns["s-1"] = {"started_at": time.time(), "message": "x"}
        assert "nisam preuzeo" in iface.busy_notice("s-1")

    def test_a_long_request_is_trimmed(self, iface):
        iface._inflight_turns["s-1"] = {
            "started_at": time.time(),
            "message": "rijec " * 60,
        }
        assert len(iface.busy_notice("s-1")) < 200

    def test_it_still_answers_when_nothing_was_recorded(self, iface):
        assert iface.busy_notice("unknown-session")


class TestChatDoesNotHangBehindTheLock:
    @pytest.mark.asyncio
    async def test_the_second_message_is_answered_quickly(self, iface, monkeypatch):
        started = asyncio.Event()

        async def slow_turn(self, user_id, message, session_id, *a, **kw):
            started.set()
            await asyncio.sleep(5)
            return {"response": "gotovo", "session_id": session_id, "timestamp": 0}

        monkeypatch.setattr(WebInterface, "_chat_locked", slow_turn)

        first = asyncio.create_task(iface.chat("u", "istrazi nesto dugo"))
        await started.wait()

        began = time.monotonic()
        second = await iface.chat("u", "Je li gotovo?")
        waited = time.monotonic() - began

        assert "nisam preuzeo" in second["response"]
        assert waited < 3, f"waited {waited:.1f}s -- it queued instead of answering"
        assert "istrazi nesto dugo" in second["response"]

        first.cancel()

    @pytest.mark.asyncio
    async def test_the_lock_is_released_so_the_next_turn_runs(self, iface, monkeypatch):
        """A timed-out acquire must not leave the lock or the record behind."""
        async def quick_turn(self, user_id, message, session_id, *a, **kw):
            return {"response": f"ok: {message}", "session_id": session_id, "timestamp": 0}

        monkeypatch.setattr(WebInterface, "_chat_locked", quick_turn)

        result = await iface.chat("u", "prvo")
        assert result["response"] == "ok: prvo"
        assert not iface.processing_locks["s-1"].locked()
        assert "s-1" not in iface._inflight_turns

        again = await iface.chat("u", "drugo")
        assert again["response"] == "ok: drugo"

    @pytest.mark.asyncio
    async def test_a_quick_pair_still_queues_normally(self, iface, monkeypatch):
        """Being told "busy" after a blink would be worse than waiting."""
        async def brief_turn(self, user_id, message, session_id, *a, **kw):
            await asyncio.sleep(GRACE / 4)
            return {"response": f"ok: {message}", "session_id": session_id, "timestamp": 0}

        monkeypatch.setattr(WebInterface, "_chat_locked", brief_turn)

        both = await asyncio.gather(
            iface.chat("u", "prvo"), iface.chat("u", "drugo")
        )
        assert [r["response"] for r in both] == ["ok: prvo", "ok: drugo"]
