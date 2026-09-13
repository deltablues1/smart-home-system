"""
Slow voice tasks must outlive the window they were asked in.

A phone's Assist dialogue ends when the screen sleeps and takes the pipeline
run with it, so an orchestrator run that outlasts the grace period stops being
the turn's answer: the user is told it continues, and the result is pushed to
the phone when it lands.

Run with:
    pytest tests/unit/test_voice_late_answer.py -v
"""

import asyncio

import pytest

from interfaces.base_interface import BaseInterface, TurnContext
from services import late_answer


def _ctx(helper=None, session_id="voice-session", user_id="ha-assist"):
    """The turn's context, as _prepare_turn would have built it."""
    return TurnContext(session_id=session_id, user_id=user_id, helper=helper)


class _StubSystem:
    philosophy_keywords = {"filozofij", "sokrat"}

    def __init__(self, delay=0.0, answer="gotovo", error=None):
        self.delay = delay
        self.answer = answer
        self.error = error
        self.prompts = []
        self.helpers = []

    async def run_orchestration(self, prompt, helper=None, user_id=None):
        self.prompts.append(prompt)
        self.helpers.append(helper)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer


class _VoiceInterface(BaseInterface):
    def __init__(self, system):
        super().__init__(session_prefix="test")
        self.system = system

    async def start(self) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    def format_response(self, response: str) -> str:  # pragma: no cover - unused
        return response


@pytest.fixture
def delivered(monkeypatch):
    """Capture what would have gone to the phone."""
    sent = []
    monkeypatch.setattr(
        late_answer, "deliver_sync", lambda question, answer: sent.append((question, answer)) or True
    )
    return sent


class TestDeferral:

    @pytest.mark.asyncio
    async def test_a_quick_answer_is_still_spoken_in_the_turn(self, monkeypatch, delivered):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "5")
        iface = _VoiceInterface(_StubSystem(answer="Upalio sam svjetlo."))

        result = await iface._run_orchestration_for_voice(
            "prompt", user_id="ha-assist", question="upali svjetlo", ctx=_ctx()
        )

        assert result == "Upalio sam svjetlo."
        assert delivered == []

    @pytest.mark.asyncio
    async def test_a_slow_answer_is_promised_now_and_pushed_later(self, monkeypatch, delivered):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        monkeypatch.setenv("VOICE_DEFER_ACK_TEXT", "Javit ću ti.")
        system = _StubSystem(delay=0.2, answer="Dizalice topline koštaju od 3.100 eura.")
        iface = _VoiceInterface(system)

        result = await iface._run_orchestration_for_voice(
            "prompt", user_id="ha-assist", question="istraži dizalice topline",
            ctx=_ctx(),
        )

        assert result == "Javit ću ti."
        # The work carries on past the turn that gave up waiting for it.
        await asyncio.sleep(0.5)
        assert delivered == [("istraži dizalice topline", "Dizalice topline koštaju od 3.100 eura.")]

    @pytest.mark.asyncio
    async def test_a_failed_deferred_task_still_reaches_the_user(self, monkeypatch, delivered):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        iface = _VoiceInterface(_StubSystem(delay=0.2, error=RuntimeError("Gmail 403")))

        await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="pošalji mail", ctx=_ctx()
        )
        await asyncio.sleep(0.5)

        assert len(delivered) == 1 and "Gmail 403" in delivered[0][1]

    @pytest.mark.asyncio
    async def test_the_wake_word_never_defers(self, monkeypatch, delivered):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        iface = _VoiceInterface(_StubSystem(delay=0.2, answer="spori odgovor"))

        # The speaker is in the room and the wake loop waits for it.
        result = await iface._run_orchestration_for_voice(
            "p", user_id="rpi-voice-1", question="istraži nešto", ctx=_ctx()
        )

        assert result == "spori odgovor"
        assert delivered == []

    @pytest.mark.asyncio
    async def test_deferral_can_be_switched_off(self, monkeypatch, delivered):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0")
        iface = _VoiceInterface(_StubSystem(delay=0.1, answer="spori odgovor"))

        result = await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="istraži nešto", ctx=_ctx()
        )

        assert result == "spori odgovor"


class TestTheDeferredTaskKeepsItsOwnRunner:
    """The reason the context is passed rather than read off self.

    `ensure_future` queues the coroutine; its body runs minutes later. Reading
    `self.system.orchestrator_helper` at that point picks up whichever runner
    the NEXT turn installed — so a slow research answer could be written into
    somebody else's session, under somebody else's user id.
    """

    @pytest.mark.asyncio
    async def test_a_later_turn_cannot_steal_the_deferred_run(
        self, monkeypatch, delivered
    ):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        system = _StubSystem(delay=0.3, answer="gotovo")
        iface = _VoiceInterface(system)

        mine = object()
        result = await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="istraži nešto",
            ctx=_ctx(helper=mine, session_id="session-A"),
        )
        assert result != "gotovo"  # deferred

        # A second turn arrives and installs its own runner while the first
        # is still working.
        system.orchestrator_helper = object()
        await asyncio.sleep(0.5)

        assert system.helpers == [mine], (
            "the deferred run used a runner it was not given"
        )

    @pytest.mark.asyncio
    async def test_the_binding_happens_when_the_callable_is_made(self):
        # Sharper than the test above, which records the helper before any
        # waiting and so mostly proves the argument was passed. Here the
        # shared runner is replaced BETWEEN making the callable and running
        # it — the exact scheduling gap that ensure_future opens, since a
        # queued coroutine reads self.* only when its body finally executes.
        system = _StubSystem(answer="gotovo")
        iface = _VoiceInterface(system)

        mine = object()
        run = iface._orchestrate(_ctx(helper=mine, session_id="session-A"))

        # A later turn arrives and installs its own runner.
        system.orchestrator_helper = object()

        await run("p")

        assert system.helpers == [mine]


class TestNotificationPayload:

    def test_the_question_titles_the_notification_and_the_answer_is_capped(self, monkeypatch):
        monkeypatch.delenv("LATE_ANSWER_MODE", raising=False)
        monkeypatch.setenv("LATE_ANSWER_MAX_CHARS", "200")

        payload = late_answer.build_payload("koliko koštaju dizalice topline", "x" * 500)

        assert payload["title"] == "Jarvis: koliko koštaju dizalice topline"
        assert len(payload["message"]) == 200 and payload["message"].endswith("…")

    def test_a_long_question_is_trimmed_into_the_title(self, monkeypatch):
        monkeypatch.delenv("LATE_ANSWER_MODE", raising=False)

        payload = late_answer.build_payload("a" * 120, "odgovor")

        assert len(payload["title"]) <= len("Jarvis: ") + 60

    def test_speak_mode_sends_the_answer_as_tts(self, monkeypatch):
        monkeypatch.setenv("LATE_ANSWER_MODE", "speak")

        payload = late_answer.build_payload("pitanje", "izgovori ovo")

        assert payload["message"] == "TTS"
        assert payload["data"]["tts_text"] == "izgovori ovo"


class TestDeliveryIsBestEffort:

    def test_it_posts_to_the_configured_service(self, monkeypatch):
        monkeypatch.setenv("HA_NOTIFY_SERVICE", "notify.mobile_app_phone")
        monkeypatch.delenv("LATE_ANSWER_MODE", raising=False)
        seen = {}

        import tools.adk_tools.ha_adk_tools as ha

        monkeypatch.setattr(ha, "_ha_request", lambda path, payload=None, **kw: seen.update(
            {"path": path, "payload": payload}))

        assert late_answer.deliver_sync("pitanje", "odgovor") is True
        assert seen["path"] == "/api/services/notify/mobile_app_phone"
        assert seen["payload"]["message"] == "odgovor"

    def test_a_broken_notify_service_is_logged_not_raised(self, monkeypatch):
        monkeypatch.setenv("HA_NOTIFY_SERVICE", "not-a-service")

        assert late_answer.deliver_sync("pitanje", "odgovor") is False

    def test_an_unreachable_home_assistant_is_survivable(self, monkeypatch):
        monkeypatch.setenv("HA_NOTIFY_SERVICE", "notify.mobile_app_phone")
        import tools.adk_tools.ha_adk_tools as ha

        def _boom(path, payload=None, **kw):
            raise RuntimeError("HA API nedostupan")

        monkeypatch.setattr(ha, "_ha_request", _boom)

        assert late_answer.deliver_sync("pitanje", "odgovor") is False
