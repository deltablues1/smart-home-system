"""
A long voice request must outlive the turn, without trampling the next one.

Deferral already worked: HA Assist gives up long before a research task
finishes, so the user is told it will arrive on the phone and `late_answer`
delivers it. Two things did not work.

* The work was an `asyncio.Task` nobody kept a record of. A deploy or a crash
  during those minutes dropped a promise that had already been spoken aloud,
  leaving nothing to point at afterwards.
* The session lock is released the moment the deferred ack goes out, so a
  follow-up a minute later started a second orchestrator over the same ADK
  session while the first was still writing to it — two conversations
  interleaved into one transcript.

The claim is narrow on purpose: it covers orchestrator runs, which share the
ADK history. Lights and sensors never touch it, so the house keeps working
while a research job runs.

No Firestore, no LLM. The job store is redirected to a temp file by the
autouse fixture in tests/conftest.py.

Run with:
    pytest tests/unit/test_background_jobs.py -v
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from interfaces.base_interface import BaseInterface, TurnContext
from services import background_jobs, late_answer


class _StubSystem:
    def __init__(self, delay=0.0, answer="gotovo"):
        self.delay = delay
        self.answer = answer
        self.runs = 0

    async def run_orchestration(self, prompt, helper=None, user_id=None):
        self.runs += 1
        if self.delay:
            await asyncio.sleep(self.delay)
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


def _ctx(session_id="voice-session", user_id="ha-assist"):
    return TurnContext(session_id=session_id, user_id=user_id, helper=object())


@pytest.fixture
def no_delivery(monkeypatch):
    monkeypatch.setattr(late_answer, "deliver_sync", lambda question, answer: True)


# --- the record ---------------------------------------------------------------

class TestTheStore:

    def test_a_running_job_holds_its_session(self):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        held = background_jobs.active_for_session("s1")
        assert held is not None and held["job_id"] == job_id
        assert background_jobs.active_for_session("s2") is None

    def test_finishing_releases_the_session(self):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")
        background_jobs.finish(job_id, answer="3.100 eura")

        assert background_jobs.active_for_session("s1") is None
        assert background_jobs.get(job_id)["answer"] == "3.100 eura"

    def test_it_survives_a_restart(self):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")
        background_jobs.finish(job_id, answer="3.100 eura")

        # Nothing cached in memory: a second reader starts from the file.
        assert background_jobs.latest_for_session("s1")["answer"] == "3.100 eura"

    def test_a_job_left_running_by_a_dead_process_is_released(self):
        background_jobs.start("s1", "ha-assist", "istraži dizalice")

        # A restart: the file remains, the in-process reservation does not.
        background_jobs.reset()

        assert background_jobs.mark_orphans_interrupted() == 1

        # Otherwise one crash would claim that conversation forever and every
        # later orchestrator turn in it would wait for a job that is gone.
        assert background_jobs.active_for_session("s1") is None
        assert background_jobs.latest_for_session("s1")["state"] == "interrupted"

    def test_a_missing_store_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKGROUND_JOBS_FILE", str(tmp_path / "nope.json"))
        assert background_jobs.active_for_session("s1") is None
        assert background_jobs.latest_for_session("s1") is None
        assert background_jobs.mark_orphans_interrupted() == 0


# --- the deferral writes one -------------------------------------------------

class TestDeferralRecordsAJob:

    @pytest.mark.asyncio
    async def test_a_deferred_run_is_written_down(self, monkeypatch, no_delivery):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        system = _StubSystem(delay=0.3, answer="3.100 eura")
        iface = _VoiceInterface(system)

        await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="istraži dizalice", ctx=_ctx()
        )

        held = background_jobs.active_for_session("voice-session")
        assert held is not None
        assert held["question"] == "istraži dizalice"

        await asyncio.sleep(0.4)

        finished = background_jobs.latest_for_session("voice-session")
        assert finished["state"] == "done"
        assert finished["answer"] == "3.100 eura"
        assert background_jobs.active_for_session("voice-session") is None

    @pytest.mark.asyncio
    async def test_a_failed_run_is_recorded_as_failed(self, monkeypatch, no_delivery):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")

        class _Failing(_StubSystem):
            async def run_orchestration(self, prompt, helper=None, user_id=None):
                await asyncio.sleep(0.2)
                raise RuntimeError("Gmail 403")

        iface = _VoiceInterface(_Failing())

        await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="pošalji mail", ctx=_ctx()
        )
        await asyncio.sleep(0.4)

        assert background_jobs.latest_for_session("voice-session")["state"] == "failed"

    @pytest.mark.asyncio
    async def test_a_quick_answer_records_nothing(self, monkeypatch, no_delivery):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "5")
        iface = _VoiceInterface(_StubSystem(answer="Upalio sam svjetlo."))

        result = await iface._run_orchestration_for_voice(
            "p", user_id="ha-assist", question="upali svjetlo", ctx=_ctx()
        )

        # Nothing was promised, so there is nothing to keep.
        assert result == "Upalio sam svjetlo."
        assert background_jobs.latest_for_session("voice-session") is None


# --- the next turn ------------------------------------------------------------

class TestTheNextTurnDoesNotWalkIn:

    @pytest.mark.asyncio
    async def test_a_second_orchestrator_run_is_refused_while_a_job_holds_it(
        self, monkeypatch, no_delivery
    ):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        system = _StubSystem(delay=0.4, answer="prvi odgovor")
        iface = _VoiceInterface(system)

        await iface._run_orchestration_for_voice(
            "p1", user_id="ha-assist", question="istraži dizalice", ctx=_ctx()
        )
        assert system.runs == 1

        second = await iface._run_orchestration_for_voice(
            "p2", user_id="ha-assist", question="a što s bojlerom", ctx=_ctx()
        )

        # Before this, the second run wrote into the same ADK session as the
        # first and the transcript became two interleaved conversations.
        assert system.runs == 1
        assert "Još radim" in second

        await asyncio.sleep(0.5)

    @pytest.mark.asyncio
    async def test_another_conversation_is_unaffected(
        self, monkeypatch, no_delivery
    ):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        system = _StubSystem(delay=0.4)
        iface = _VoiceInterface(system)

        await iface._run_orchestration_for_voice(
            "p1", user_id="ha-assist", question="istraži dizalice",
            ctx=_ctx(session_id="session-A"),
        )

        await iface._run_orchestration_for_voice(
            "p2", user_id="ha-assist", question="drugo pitanje",
            ctx=_ctx(session_id="session-B"),
        )

        assert system.runs == 2
        await asyncio.sleep(0.6)

    @pytest.mark.asyncio
    async def test_the_session_frees_up_once_the_job_ends(
        self, monkeypatch, no_delivery
    ):
        monkeypatch.setenv("VOICE_DEFER_AFTER_SECONDS", "0.05")
        system = _StubSystem(delay=0.15)
        iface = _VoiceInterface(system)

        await iface._run_orchestration_for_voice(
            "p1", user_id="ha-assist", question="istraži dizalice", ctx=_ctx()
        )
        await asyncio.sleep(0.3)

        await iface._run_orchestration_for_voice(
            "p2", user_id="ha-assist", question="drugo pitanje", ctx=_ctx()
        )

        assert system.runs == 2


# --- "je li gotovo?" ----------------------------------------------------------

class TestAskingAfterAJob:

    @pytest.mark.parametrize("message", [
        "je li gotovo?", "jesi li završio", "ima li novosti", "kako napreduje",
    ])
    def test_status_questions_are_recognised(self, message):
        assert BaseInterface._is_job_status_question(message) is True

    @pytest.mark.parametrize("message", [
        "istraži dizalice topline",
        "je li gotovo pranje rublja u perilici",
        "upali svjetlo",
    ])
    def test_ordinary_requests_are_not(self, message):
        # A loose match would swallow real requests and answer them with a
        # status report, which is worse than not having the lane.
        assert BaseInterface._is_job_status_question(message) is False

    def test_the_answer_comes_from_the_record(self):
        iface = _VoiceInterface(_StubSystem())
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        running = iface._answer_job_status("s1")
        assert "Još radim" in running

        background_jobs.finish(job_id, answer="Od 3.100 eura.")
        assert iface._answer_job_status("s1") == "Od 3.100 eura."

    def test_nothing_to_report_falls_through(self):
        iface = _VoiceInterface(_StubSystem())
        assert iface._answer_job_status("s1") is None

    def test_an_interrupted_job_says_so(self):
        background_jobs.start("s1", "ha-assist", "istraži dizalice")
        background_jobs.reset()  # the process that owned it is gone
        background_jobs.mark_orphans_interrupted()

        iface = _VoiceInterface(_StubSystem())
        answer = iface._answer_job_status("s1")

        # The promise was spoken out loud; silence afterwards is the worst
        # possible answer.
        assert "prekinuo" in answer

    def test_a_long_running_job_reports_how_long(self, monkeypatch):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")
        background_jobs._JOBS[job_id]["started_at"] = time.time() - 300

        answer = background_jobs.describe(background_jobs.get(job_id))
        assert "5 min" in answer


class TestAFailedWriteKeepsTheReservation:
    """Losing the notepad must not also lose the protection.

    `_save` used to swallow its error and `start()` handed back a job id
    regardless. Since the reservation was read back out of that same file, a
    disk that could not be written meant the job ran with no protection at
    all — and the next turn walked straight into its conversation.
    """

    @pytest.fixture
    def unwritable(self, monkeypatch):
        def refuse(_jobs):
            raise background_jobs.JobStoreError("disk full")

        monkeypatch.setattr(background_jobs, "_save", refuse)

    def test_the_session_is_still_reserved(self, unwritable):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        held = background_jobs.active_for_session("s1")
        assert held is not None and held["job_id"] == job_id

    def test_the_job_is_marked_as_not_durable(self, unwritable):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        # It is running; it simply will not be there after a restart.
        assert background_jobs.get(job_id)["durable"] is False

    def test_finishing_still_releases_it(self, unwritable):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")
        background_jobs.finish(job_id, answer="gotovo")

        assert background_jobs.active_for_session("s1") is None


class TestEachProcessOwnsItsNotepad:
    """Web, Telegram, the wake word and the scheduler all call this.

    With one shared file, any of them starting up would find another's live
    job in `running` state, declare it interrupted, and release a session that
    was still being written to.
    """

    def test_owners_do_not_see_each_other(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BACKGROUND_JOBS_FILE", raising=False)
        monkeypatch.setattr(
            background_jobs, "_store_path",
            lambda: tmp_path / f"jobs.{background_jobs._owner}.json",
        )

        background_jobs.set_owner("web")
        background_jobs.start("s1", "ha-assist", "istraži dizalice")

        background_jobs.set_owner("scheduler")
        assert background_jobs._load() == {}

        background_jobs.set_owner("web")
        assert "s1" in {j["session_id"] for j in background_jobs._load().values()}

    def test_a_colleague_starting_up_does_not_interrupt_a_live_job(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("BACKGROUND_JOBS_FILE", raising=False)
        monkeypatch.setattr(
            background_jobs, "_store_path",
            lambda: tmp_path / f"jobs.{background_jobs._owner}.json",
        )

        background_jobs.set_owner("web")
        background_jobs.start("s1", "ha-assist", "istraži dizalice")

        # The scheduler process comes up and reclaims its own orphans.
        background_jobs.set_owner("scheduler")
        assert background_jobs.mark_orphans_interrupted() == 0

        background_jobs.set_owner("web")
        assert background_jobs.active_for_session("s1") is not None

    def test_a_live_job_in_this_process_is_never_an_orphan(self):
        background_jobs.start("s1", "ha-assist", "istraži dizalice")

        # Same process, same run: this job is not from a dead one.
        assert background_jobs.mark_orphans_interrupted() == 0
        assert background_jobs.active_for_session("s1") is not None


class TestAFinishedJobWinsOverAStaleRow:
    """The failing write can be the one that records the END.

    start() writes `running`, the job finishes, and the write that would have
    recorded that fails. Dropping the in-process record then left the stale
    `running` row on disk as the only answer, and a conversation that was
    already free stayed reserved.
    """

    def test_a_finished_job_does_not_keep_reserving_its_session(
        self, monkeypatch
    ):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")
        assert background_jobs.active_for_session("s1") is not None

        def refuse(_jobs):
            raise background_jobs.JobStoreError("disk full")

        monkeypatch.setattr(background_jobs, "_save", refuse)
        background_jobs.finish(job_id, answer="3.100 eura")

        assert background_jobs.active_for_session("s1") is None

    def test_asking_after_it_gets_the_real_answer(self, monkeypatch):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        def refuse(_jobs):
            raise background_jobs.JobStoreError("disk full")

        monkeypatch.setattr(background_jobs, "_save", refuse)
        background_jobs.finish(job_id, answer="3.100 eura")

        # Not "still working on it" for something that finished.
        assert background_jobs.get(job_id)["state"] == "done"
        assert background_jobs.latest_for_session("s1")["answer"] == "3.100 eura"

    def test_the_lost_record_is_marked_as_such(self, monkeypatch):
        job_id = background_jobs.start("s1", "ha-assist", "istraži dizalice")

        def refuse(_jobs):
            raise background_jobs.JobStoreError("disk full")

        monkeypatch.setattr(background_jobs, "_save", refuse)
        background_jobs.finish(job_id, answer="gotovo")

        assert background_jobs.get(job_id)["durable"] is False

    def test_memory_does_not_grow_for_the_life_of_the_process(self):
        for n in range(background_jobs._MEMORY_MAX + 20):
            job_id = background_jobs.start(f"s{n}", "ha-assist", "x")
            background_jobs.finish(job_id, answer="ok")

        assert len(background_jobs._JOBS) <= background_jobs._MEMORY_MAX

    def test_a_running_job_is_never_dropped_from_memory(self):
        keep = background_jobs.start("held", "ha-assist", "dugi zadatak")

        for n in range(background_jobs._MEMORY_MAX + 20):
            job_id = background_jobs.start(f"s{n}", "ha-assist", "x")
            background_jobs.finish(job_id, answer="ok")

        # It is the reservation; losing it would free a conversation that is
        # still being written to.
        assert keep in background_jobs._JOBS
        assert background_jobs.active_for_session("held") is not None
