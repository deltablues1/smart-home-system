"""
Every chat door must prepare a turn the same way.

Streaming used to build its own session context and skip the approval half
entirely: `approvals.set_session()` was never called on that path, so the
ContextVar stayed at "global", `on_user_turn()` never ran, and a "da" typed
into the web UI could not authorise anything — the gate just escalated to a
hard stop. Both paths now go through `BaseInterface._prepare_turn()`.

Two neighbours are covered here because they were the same accident:

* `chat_stream`'s CLASSROOM fallback called `chat()` while already holding
  that session's lock. `asyncio.Lock` is not reentrant, so the request blocked
  on itself forever and never released it.
* `active_mode` was flipped to CLASSROOM by a substring keyword match. The
  stems "etik" and "logik" matched "etiketu" and "logiku", and the mode is
  global — a phrase said to HA Assist left the browser answering as Socrates,
  and the stream deadlocked on the way in.

Pure interface logic — no Firestore, no LLM, no ADK runner.

Run with:
    pytest tests/unit/test_shared_turn_path.py -v
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from interfaces.base_interface import looks_philosophical
from interfaces.web_interface import WebInterface
from services import approvals


# --- stubs ------------------------------------------------------------------

def _text_event(text: str, author: str = "orchestrator"):
    """An ADK-shaped event carrying one text part."""
    part = SimpleNamespace(
        text=text, inline_data=None, function_call=None, function_response=None
    )
    return SimpleNamespace(author=author, content=SimpleNamespace(parts=[part]))


class _StubPersistence:
    """Every persistence call is an awaitable no-op."""

    def __getattr__(self, _name):
        async def _noop(*args, **kwargs):
            return None
        return _noop


class _StubHelper:
    def __init__(self, session_id: str = "seed-session"):
        self.session_id = session_id
        self.session_service = None
        # What a tool would see if it asked mid-run. This is the whole point:
        # tools run inside stream(), so this is the context that matters.
        self.seen_sessions = []
        self.streamed_content = None

    async def stream(self, message: str):
        self.seen_sessions.append(approvals.current_session())
        yield _text_event("odgovor")

    async def stream_content(self, content):
        self.streamed_content = content
        async for event in self.stream(""):
            yield event

    async def run(self, message: str) -> str:
        self.seen_sessions.append(approvals.current_session())
        return "odgovor"

    def record_exchange(self, *args, **kwargs):
        pass


class _StubSystem:
    def __init__(self):
        self.active_mode = "LEGACY"
        self.session_id = "seed-session"
        self.user_id = "seed-user"
        self.orchestrator = object()
        self.orchestrator_helper = _StubHelper()
        self.socrates = SimpleNamespace(run_with_fallback=self._socrates)
        self.termination_checker = SimpleNamespace(run_with_fallback=self._checker)

    async def _socrates(self, message: str) -> str:
        return "Sokrat pita: sto je vrlina?"

    async def _checker(self, message: str) -> str:
        return "CONTINUE"

    async def run_orchestration(self, message: str, helper=None, user_id=None) -> str:
        (helper or self.orchestrator_helper).seen_sessions.append(
            approvals.current_session()
        )
        return f"orkestrator: {message}"


class _TestWebInterface(WebInterface):
    """WebInterface with the system, persistence and runner binding stubbed."""

    def __init__(self):
        super().__init__()
        self.system = _StubSystem()
        self._persistence = _StubPersistence()

    def initialize_system(self) -> None:
        pass

    def _helper_for(self, session_id: str, user_id: str):
        self.system.orchestrator_helper.session_id = session_id
        return self.system.orchestrator_helper


@pytest.fixture
def iface():
    approvals.reset()
    approvals.set_session("global")
    try:
        yield _TestWebInterface()
    finally:
        approvals.reset()
        approvals.set_session("global")


# --- word-boundary routing --------------------------------------------------

class TestPhilosophyMatching:

    @pytest.mark.parametrize("message", [
        "napravi etiketu za proizvod",
        "provjeri logiku rasporeda",
        "kakva je logika ovog koda",
        "izbaci kantu za smece",
        "koliko kisika ima u zraku",
        "posalji ponudu klijentu",
    ])
    def test_ordinary_croatian_is_not_philosophy(self, message):
        assert looks_philosophical(message) is False

    @pytest.mark.parametrize("message", [
        "objasni mi Kantov kategoricki imperativ",
        "sto bi Sokrat rekao o ovome",
        "zanima me filozofija uma",
        "etika vrline kod Aristotela",
        "poslovna etika i odgovornost",
        "nietzsche i vjecno vracanje",
    ])
    def test_real_philosophy_still_routes(self, message):
        assert looks_philosophical(message) is True

    def test_diacritics_do_not_matter(self):
        assert looks_philosophical("eticko pitanje") is True
        assert looks_philosophical("etičko pitanje") is True


# --- global mode is only changed on purpose ---------------------------------

class TestClassroomModeIsExplicit:

    @pytest.mark.asyncio
    async def test_philosophy_question_answers_without_flipping_the_mode(self, iface):
        reply = await iface.process_message(
            "web-user", "objasni mi Kantov imperativ", session_id="s1"
        )
        assert "Sokrat" in reply
        assert iface.system.active_mode == "LEGACY"

    @pytest.mark.asyncio
    async def test_label_request_reaches_the_orchestrator(self, iface):
        reply = await iface.process_message(
            "web-user", "napravi etiketu za proizvod", session_id="s2"
        )
        assert reply.startswith("orkestrator:")
        assert iface.system.active_mode == "LEGACY"

    @pytest.mark.asyncio
    async def test_explicit_command_enters_and_leaves(self, iface):
        await iface.process_message("web-user", "/classroom", session_id="s3")
        assert iface.system.active_mode == "CLASSROOM"

        await iface.process_message("web-user", "/leave", session_id="s3")
        assert iface.system.active_mode == "LEGACY"


# --- the streaming path is not a second implementation ----------------------

class TestStreamingSharesThePath:

    @pytest.mark.asyncio
    async def test_classroom_stream_does_not_deadlock(self, iface):
        iface.system.active_mode = "CLASSROOM"

        events = []

        async def drain():
            async for event in iface.chat_stream("web-user", "sto je vrlina"):
                events.append(event)

        # Before the fix this blocked on its own session lock and never
        # returned, so a timeout is the assertion.
        await asyncio.wait_for(drain(), timeout=5)

        assert events[0]["event"] == "text"
        assert events[-1]["event"] == "done"

    @pytest.mark.asyncio
    async def test_stream_binds_the_real_session_not_global(self, iface):
        async for _ in iface.chat_stream("web-user", "pozdrav"):
            pass

        session_id = iface.user_sessions["web-user"]
        seen = iface.system.orchestrator_helper.seen_sessions
        assert seen, "the stub runner never ran"
        assert seen[-1] == session_id
        assert seen[-1] != "global"

    @pytest.mark.asyncio
    async def test_yes_in_the_stream_arms_a_pending_approval(self, iface):
        session_id = iface.get_or_create_session("web-user")
        action_id = "gmail_send_message:deadbeef"
        approvals.register(
            action_id, question="poslati mail na ivan@example.com?", session=session_id
        )

        # Registered is not armed: acting now would be acting unasked.
        assert approvals.redeem(action_id, session=session_id) is False

        async for _ in iface.chat_stream("web-user", "da"):
            pass

        assert approvals.redeem(action_id, session=session_id) is True

    @pytest.mark.asyncio
    async def test_non_affirmative_reply_in_the_stream_cancels(self, iface):
        session_id = iface.get_or_create_session("web-user")
        action_id = "gmail_send_message:cafe"
        approvals.register(action_id, question="poslati mail?", session=session_id)

        async for _ in iface.chat_stream("web-user", "ne, nemoj"):
            pass

        assert approvals.redeem(action_id, session=session_id) is False

    @pytest.mark.asyncio
    async def test_the_mode_command_works_through_the_stream_too(self, iface):
        # The command handling first lived only in process_message, so
        # /classroom typed in the browser — which posts to /api/chat/stream —
        # went to the runner as an ordinary message and changed nothing.
        events = []
        async for event in iface.chat_stream("web-user", "/classroom"):
            events.append(event)

        assert iface.system.active_mode == "CLASSROOM"
        assert events[0]["event"] == "text"
        assert "učionic" in events[0]["data"]
        assert events[-1]["event"] == "done"
        # It answered itself; the runner was never asked.
        assert iface.system.orchestrator_helper.seen_sessions == []

    @pytest.mark.asyncio
    async def test_leaving_works_through_the_stream_too(self, iface):
        async for _ in iface.chat_stream("web-user", "/classroom"):
            pass
        assert iface.system.active_mode == "CLASSROOM"

        # Now in CLASSROOM the stream falls back to _chat_locked, which runs
        # process_message — the command must still be recognised there.
        async for _ in iface.chat_stream("web-user", "/leave"):
            pass

        assert iface.system.active_mode == "LEGACY"

    @pytest.mark.asyncio
    async def test_a_mode_command_still_prepares_the_turn_once(self, iface):
        session_id = iface.get_or_create_session("web-user")
        action_id = "gmail_send_message:facade"
        approvals.register(action_id, question="poslati mail?", session=session_id)

        # "/classroom" is not a yes, so it must cancel the pending approval —
        # exactly once. Arming twice in one turn would eat a later "da".
        async for _ in iface.chat_stream("web-user", "/classroom"):
            pass

        assert approvals.redeem(action_id, session=session_id) is False
        assert approvals.current_session() == session_id

    @pytest.mark.asyncio
    async def test_the_user_message_is_still_recorded(self, iface):
        session_id = iface.get_or_create_session("web-user")

        async for _ in iface.chat_stream("web-user", "/classroom"):
            pass

        roles = [m["role"] for m in iface.message_history[session_id]]
        assert roles == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_both_doors_bind_the_same_session(self, iface):
        session_id = iface.get_or_create_session("web-user")

        await iface.chat("web-user", "pozdrav")
        from_chat = iface.system.orchestrator_helper.seen_sessions[-1]

        async for _ in iface.chat_stream("web-user", "pozdrav"):
            pass
        from_stream = iface.system.orchestrator_helper.seen_sessions[-1]

        assert from_chat == from_stream == session_id


# --- one runner per session, not one runner ---------------------------------

class _RealBindingInterface(_TestWebInterface):
    """Same stubs, but `_helper_for` is the real one under test."""

    _helper_for = WebInterface._helper_for


class _FakeRunner:
    def __init__(self, **kwargs):
        self.session_id = kwargs["session_id"]
        self.user_id = kwargs["user_id"]
        self.session_service = kwargs.get("session_service")


@pytest.fixture
def binding_iface(monkeypatch):
    import agents.adk_agents.runner_utils as runner_utils

    monkeypatch.setattr(runner_utils, "RunnerHelper", _FakeRunner)
    approvals.reset()
    iface = _RealBindingInterface()
    iface.system.orchestrator = object()
    iface.system.orchestrator_helper = None
    try:
        yield iface
    finally:
        approvals.reset()
        approvals.set_session("global")


class TestOneRunnerPerSession:

    @pytest.mark.asyncio
    async def test_two_sessions_hold_two_runners_at_once(self, binding_iface):
        # There used to be one `system.orchestrator_helper`, rebound on every
        # turn. Two conversations overwrote each other's runner, and whichever
        # spoke last owned it.
        first = await binding_iface._prepare_turn("ana", "pozdrav", "session-A")
        second = await binding_iface._prepare_turn("ivan", "pozdrav", "session-B")

        assert first.helper is not second.helper
        assert first.helper.session_id == "session-A"
        assert second.helper.session_id == "session-B"

        # The first conversation's runner still belongs to the first
        # conversation after the second one has been through.
        again = await binding_iface._prepare_turn("ana", "i dalje ja", "session-A")
        assert again.helper is first.helper

    @pytest.mark.asyncio
    async def test_the_cache_does_not_grow_without_bound(self, binding_iface):
        cap = _RealBindingInterface._MAX_CACHED_HELPERS
        for n in range(cap + 5):
            await binding_iface._prepare_turn("ana", "x", f"session-{n}")

        assert len(binding_iface._runner_helpers) <= cap
        # The newest session is still there; the oldest is what went.
        assert f"session-{cap + 4}" in binding_iface._runner_helpers
        assert "session-0" not in binding_iface._runner_helpers

    @pytest.mark.asyncio
    async def test_the_turn_context_carries_its_own_user(self, binding_iface):
        ctx = await binding_iface._prepare_turn("ana", "pozdrav", "session-A")

        assert ctx.user_id == "ana"
        assert ctx.session_id == "session-A"
        # Bound for flows that must name who is being asked — the web
        # approval flow used a process-global env var for this, written only
        # by the streaming path.
        assert approvals.current_user() == "ana"


class TestBothDoorsChooseTheSameOrchestration:
    """USE_PLAN_EXECUTE used to apply to `/api/chat` only.

    The browser posts to `/api/chat/stream`, which drove the runner directly
    and never went through `run_orchestration` — so turning the flag on
    changed what HA Assist did and left the browser exactly as it was.
    """

    @pytest.mark.asyncio
    async def test_streaming_uses_plan_execute_when_it_applies(
        self, iface, monkeypatch
    ):
        monkeypatch.setenv("USE_PLAN_EXECUTE", "true")
        request = (
            "napravi dokument s ponudom za klijenta, pa mi ga posalji na mail "
            "i dodaj redak u tablicu prodaje"
        )

        events = []
        async for event in iface.chat_stream("web-user", request):
            events.append(event)

        texts = [e["data"] for e in events if e["event"] == "text"]
        assert any(t.startswith("orkestrator:") for t in texts), (
            "the streaming path did not go through run_orchestration"
        )
        assert events[-1]["event"] == "done"

    @pytest.mark.asyncio
    async def test_streaming_still_streams_a_single_step_request(
        self, iface, monkeypatch
    ):
        monkeypatch.setenv("USE_PLAN_EXECUTE", "true")

        events = []
        async for event in iface.chat_stream("web-user", "upali svjetlo"):
            events.append(event)

        texts = [e["data"] for e in events if e["event"] == "text"]
        assert texts == ["odgovor"], "a short request must still stream"

    @pytest.mark.asyncio
    async def test_the_flag_off_streams_everything(self, iface, monkeypatch):
        monkeypatch.setenv("USE_PLAN_EXECUTE", "false")
        request = (
            "napravi dokument s ponudom za klijenta, pa mi ga posalji na mail "
            "i dodaj redak u tablicu prodaje"
        )

        events = []
        async for event in iface.chat_stream("web-user", request):
            events.append(event)

        texts = [e["data"] for e in events if e["event"] == "text"]
        assert texts == ["odgovor"]


class TestAttachmentsAreNeverDroppedSilently:
    """Plan-execute takes a plain string; an image has nowhere to go in it.

    Routing a request with an attachment down that branch loses the picture
    without saying so — "analiziraj priloženu sliku pa napravi dokument"
    arrives as text alone and the answer is confidently about nothing.
    """

    @staticmethod
    def _image():
        # 1x1 PNG, enough for types.Part.from_bytes to accept.
        return {
            "base64": (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8"
                "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            ),
            "mime_type": "image/png",
        }

    @pytest.mark.asyncio
    async def test_a_multi_step_request_with_an_image_keeps_streaming(
        self, iface, monkeypatch
    ):
        monkeypatch.setenv("USE_PLAN_EXECUTE", "true")
        request = (
            "analiziraj priloženu sliku pa napravi dokument s nalazom "
            "i posalji mi ga na mail kad zavrsis"
        )

        events = []
        async for event in iface.chat_stream(
            "web-user", request, attachments=[self._image()]
        ):
            events.append(event)

        texts = [e["data"] for e in events if e["event"] == "text"]
        assert texts == ["odgovor"], (
            "the request went to plan-execute, which cannot carry the image"
        )

    @pytest.mark.asyncio
    async def test_the_image_actually_reaches_the_runner(self, iface):
        events = []
        async for event in iface.chat_stream(
            "web-user", "što je na slici?", attachments=[self._image()]
        ):
            events.append(event)

        content = iface.system.orchestrator_helper.streamed_content
        assert content is not None, "stream_content was never called"
        # Text plus the image part, not text alone.
        assert len(content.parts) == 2
        assert any(getattr(p, "inline_data", None) is not None for p in content.parts)


class TestARunnerForTheWrongSessionIsAnError:

    @pytest.mark.asyncio
    async def test_a_mismatched_seed_is_refused(self, binding_iface):
        # Falling back to the seed ran the request in whatever session that
        # runner belonged to — the exact confusion the per-session cache
        # exists to end, arriving quietly instead of loudly.
        binding_iface.system.orchestrator = None
        binding_iface.system.orchestrator_helper = _FakeRunner(
            session_id="seed-session", user_id="netko-drugi"
        )

        with pytest.raises(RuntimeError, match="requested-session"):
            await binding_iface._prepare_turn("ana", "pozdrav", "requested-session")

    @pytest.mark.asyncio
    async def test_a_matching_seed_is_still_reused(self, binding_iface):
        seed = _FakeRunner(session_id="requested-session", user_id="ana")
        binding_iface.system.orchestrator = None
        binding_iface.system.orchestrator_helper = seed

        ctx = await binding_iface._prepare_turn("ana", "pozdrav", "requested-session")

        assert ctx.helper is seed


class TestABusySessionIsRefusedAtEveryDoor:
    """The reservation belongs to the conversation, not to the voice path.

    The check first lived only in `_run_orchestration_for_voice`, so opening
    the same conversation in the browser while a voice job was still writing
    to its ADK session started a second orchestrator over it anyway.
    """

    @pytest_asyncio.fixture
    async def held_session(self, iface):
        # Async so the session is registered inside a running loop: the
        # interface fires its persistence writes as tasks, and creating one
        # from a sync fixture leaves an un-awaited coroutine behind.
        from services import background_jobs

        session_id = iface.get_or_create_session("web-user")
        background_jobs.start(session_id, "ha-assist", "istraži dizalice topline")
        return session_id

    @pytest.mark.asyncio
    async def test_the_browser_stream_does_not_walk_in(self, iface, held_session):
        events = []
        async for event in iface.chat_stream("web-user", "a što s bojlerom"):
            events.append(event)

        texts = [e["data"] for e in events if e["event"] == "text"]
        assert any("Još radim" in t for t in texts)
        assert iface.system.orchestrator_helper.seen_sessions == [], (
            "the runner was started while a job still held the session"
        )
        assert events[-1]["event"] == "done"

    @pytest.mark.asyncio
    async def test_the_text_path_does_not_either(self, iface, held_session):
        reply = await iface.process_message(
            "web-user", "a što s bojlerom", session_id=held_session
        )

        assert "Još radim" in reply
        assert iface.system.orchestrator_helper.seen_sessions == []

    @pytest.mark.asyncio
    async def test_another_conversation_is_free(self, iface, held_session):
        reply = await iface.process_message(
            "web-user", "napravi etiketu", session_id="another-session"
        )

        assert reply.startswith("orkestrator:")

    @pytest.mark.asyncio
    async def test_the_session_frees_up_when_the_job_ends(self, iface, held_session):
        from services import background_jobs

        job = background_jobs.active_for_session(held_session)
        background_jobs.finish(job["job_id"], answer="3.100 eura")

        reply = await iface.process_message(
            "web-user", "a što s bojlerom", session_id=held_session
        )
        assert reply.startswith("orkestrator:")
