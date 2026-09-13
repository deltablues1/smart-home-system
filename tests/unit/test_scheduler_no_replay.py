"""
A scheduled job that already did something must not be run again.

`_execute_job` retried by re-running the entire natural-language request, up
to `max_retries + 1` times. The agent has no memory of what the previous
attempt accomplished and the request is prose, not a resumable plan — so a job
that sent the mail and then tripped on the next step sent the mail again.

The rule now: replay only while nothing has happened. `services.run_effects`
carries that one fact, recorded in `runner_utils.run_agent_simple` where every
agent run already passes.

Any tool counts, not just writes. At the orchestrator level a "tool" is
usually a whole worker agent, so a call that looks like a read is a nested run
that may have written anything — the same line `web_interface.chat_stream`
already draws for its own retry.

And the status tells the truth: a failure after a tool ran is `UNKNOWN`, not
`FAILED`. Something happened; nothing here can say what.

No APScheduler, no LLM, no Telegram.

Run with:
    pytest tests/unit/test_scheduler_no_replay.py -v
"""

import pytest

from config.scheduler_config import JobTrigger, ScheduledJob
from interfaces.scheduler_interface import SchedulerInterface
from services import run_effects


def _job(**overrides) -> ScheduledJob:
    base = dict(
        id="test-job",
        name="Test",
        agent_request="posalji dnevni izvjestaj",
        trigger=JobTrigger(type="cron", cron_expression="0 7 * * *"),
        max_retries=2,
    )
    base.update(overrides)
    return ScheduledJob(**base)


class _StubOrchestrator:
    name = "orchestrator"


class _StubSystem:
    orchestrator = _StubOrchestrator()


@pytest.fixture
def scheduler(monkeypatch, tmp_path):
    # Never touch the repo's real results file from a test.
    import interfaces.scheduler_interface as sched_mod

    saved = {}
    monkeypatch.setattr(sched_mod, "save_results", lambda r: saved.update({"r": dict(r)}))
    monkeypatch.setattr(sched_mod, "load_results", lambda: {})

    iface = SchedulerInterface()
    iface.saved_results = saved
    iface.system = _StubSystem()
    iface.initialize_system = lambda: None

    async def no_delivery(*args, **kwargs):
        return False

    monkeypatch.setattr(iface, "_deliver_to_telegram", no_delivery)
    monkeypatch.setattr(iface, "_forget_one_shot", lambda job: None)
    run_effects.clear()
    try:
        yield iface
    finally:
        run_effects.clear()


def _install_runner(monkeypatch, behaviour):
    """Replace RunnerHelper so `run()` does exactly what the test wants."""
    import agents.adk_agents.runner_utils as runner_utils

    class _Helper:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, message):
            return await behaviour(message)

    monkeypatch.setattr(runner_utils, "RunnerHelper", _Helper)


class TestReplayOnlyWhileNothingHappened:

    @pytest.mark.asyncio
    async def test_a_failure_before_any_tool_is_retried(self, scheduler, monkeypatch):
        attempts = []

        async def fails_twice_then_works(message):
            attempts.append(message)
            if len(attempts) < 3:
                raise RuntimeError("model unavailable")
            return "gotovo"

        _install_runner(monkeypatch, fails_twice_then_works)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job())

        assert len(attempts) == 3
        # Delivery is stubbed to fail, and a scheduled answer nobody received
        # is not a success — that distinction already existed for briefings
        # and now applies to every job.
        assert scheduler.job_results["test-job"]["status"] == "SUCCESS_NOT_DELIVERED"

    @pytest.mark.asyncio
    async def test_a_failure_after_a_tool_is_not_retried(self, scheduler, monkeypatch):
        attempts = []

        async def sends_then_fails(message):
            attempts.append(message)
            run_effects.note_tool_call("gmail_send_message")
            raise RuntimeError("connection reset")

        _install_runner(monkeypatch, sends_then_fails)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job())

        # The whole point: the mail was sent once, not three times.
        assert len(attempts) == 1

    @pytest.mark.asyncio
    async def test_the_status_says_unknown_not_failed(self, scheduler, monkeypatch):
        async def sends_then_fails(message):
            run_effects.note_tool_call("gmail_send_message")
            run_effects.note_tool_call("sheets_append_values")
            raise RuntimeError("connection reset")

        _install_runner(monkeypatch, sends_then_fails)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job())

        result = scheduler.job_results["test-job"]
        assert result["status"] == "UNKNOWN"
        assert result["tools_ran"] == ["gmail_send_message", "sheets_append_values"]

    @pytest.mark.asyncio
    async def test_exhausted_retries_without_tools_are_a_plain_failure(
        self, scheduler, monkeypatch
    ):
        async def always_fails(message):
            raise RuntimeError("model unavailable")

        _install_runner(monkeypatch, always_fails)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job(max_retries=1))

        result = scheduler.job_results["test-job"]
        assert result["status"] == "FAILED"
        assert result["attempt"] == 2

    @pytest.mark.asyncio
    async def test_each_attempt_starts_with_a_clean_ledger(
        self, scheduler, monkeypatch
    ):
        # A read on attempt 1 must not block attempt 2 from happening at all —
        # the ledger is per attempt, and attempt 1 is what stops there.
        seen = []

        async def reads_then_fails(message):
            seen.append(run_effects.tool_calls())
            run_effects.note_tool_call("calendar_list_events")
            raise RuntimeError("boom")

        _install_runner(monkeypatch, reads_then_fails)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job())

        assert seen == [()], "the ledger must be empty when an attempt starts"


class TestRunEffects:

    def test_inert_outside_a_run(self):
        run_effects.clear()
        run_effects.note_tool_call("gmail_send_message")
        assert run_effects.anything_happened() is False
        assert run_effects.tool_calls() == ()

    def test_records_in_order_inside_a_run(self):
        run_effects.start_run()
        run_effects.note_tool_call("drive_upload_file")
        run_effects.note_tool_call("gmail_send_message")
        assert run_effects.tool_calls() == ("drive_upload_file", "gmail_send_message")
        assert run_effects.anything_happened() is True
        run_effects.clear()

    @pytest.mark.asyncio
    async def test_a_nested_task_is_visible_to_the_caller(self):
        import asyncio

        run_effects.start_run()

        async def nested_worker():
            # A worker agent runs as its own task; it copies the context but
            # shares the list, so what it did still counts.
            run_effects.note_tool_call("sheets_append_values")

        await asyncio.create_task(nested_worker())

        assert run_effects.tool_calls() == ("sheets_append_values",)
        run_effects.clear()


async def _no_sleep(seconds):
    return None


class TestTheStatusSaysWhatActuallyHappened:
    """`helper.run()` returning without raising is not the same as done.

    Everything below used to be filed as SUCCESS: a job the approval gate
    refused because it needed a person, and a job whose answer never reached
    anyone. Both sent someone looking for a bug that was not there, or worse,
    stopped them looking at all.
    """

    @pytest.mark.asyncio
    async def test_a_job_that_needed_a_person_is_not_a_success(
        self, scheduler, monkeypatch
    ):
        async def asks_for_confirmation(message):
            run_effects.note_tool_call("gmail_send_message")
            run_effects.note_needs_confirmation("gmail_send_message")
            return "Nisam poslao mail jer traži tvoju potvrdu."

        _install_runner(monkeypatch, asks_for_confirmation)

        await scheduler._execute_job(_job())

        result = scheduler.job_results["test-job"]
        assert result["status"] == "NEEDS_CONFIRMATION"
        assert result["needs_confirmation"] == ["gmail_send_message"]

    @pytest.mark.asyncio
    async def test_a_delivered_answer_is_a_success(self, scheduler, monkeypatch):
        async def works(message):
            return "gotovo"

        _install_runner(monkeypatch, works)

        async def delivered(*args, **kwargs):
            return True

        monkeypatch.setattr(scheduler, "_deliver_to_telegram", delivered)

        await scheduler._execute_job(_job())

        assert scheduler.job_results["test-job"]["status"] == "SUCCESS"


class TestEachRunIsItsOwn:

    @pytest.mark.asyncio
    async def test_a_run_gets_an_id_and_its_own_session(
        self, scheduler, monkeypatch
    ):
        sessions = []

        import agents.adk_agents.runner_utils as runner_utils

        class _Helper:
            def __init__(self, **kwargs):
                sessions.append(kwargs["session_id"])

            async def run(self, message):
                return "gotovo"

        monkeypatch.setattr(runner_utils, "RunnerHelper", _Helper)

        await scheduler._execute_job(_job())
        first = scheduler.job_results["test-job"]["run_id"]

        await scheduler._execute_job(_job())
        second = scheduler.job_results["test-job"]["run_id"]

        assert first != second
        # The session used to be a stable "scheduler-{job_id}", so yesterday's
        # conversation was still in context when today's run started.
        assert sessions[0] != sessions[1]
        assert all(s.startswith("scheduler-test-job-") for s in sessions)


class TestResultsSurviveARestart:

    @pytest.mark.asyncio
    async def test_the_outcome_is_written_to_disk(self, scheduler, monkeypatch):
        async def works(message):
            return "gotovo"

        _install_runner(monkeypatch, works)

        await scheduler._execute_job(_job())

        # In memory only, nobody could tell after a restart whether last
        # night's job had run at all.
        assert scheduler.saved_results["r"]["test-job"]["status"] in (
            "SUCCESS", "SUCCESS_NOT_DELIVERED"
        )

    def test_results_are_loaded_back_at_startup(self, monkeypatch, tmp_path):
        import config.scheduler_config as cfg

        path = tmp_path / "results.json"
        cfg.save_results({"nightly": {"status": "UNKNOWN", "run_id": "abc"}}, path)

        assert cfg.load_results(path) == {
            "nightly": {"status": "UNKNOWN", "run_id": "abc"}
        }

    def test_a_missing_or_broken_file_is_not_fatal(self, tmp_path):
        import config.scheduler_config as cfg

        assert cfg.load_results(tmp_path / "nope.json") == {}

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert cfg.load_results(broken) == {}


class TestOneSessionServiceForTheProcess:
    """A RunnerHelper builds its own session service when given none.

    Building a helper inside the attempt loop therefore created a Firestore
    client per attempt, and nothing ever closed any of them.
    """

    @pytest.mark.asyncio
    async def test_the_attempts_of_one_run_share_a_helper(
        self, scheduler, monkeypatch
    ):
        built = []

        import agents.adk_agents.runner_utils as runner_utils

        class _Helper:
            def __init__(self, **kwargs):
                built.append(kwargs)
                self.calls = 0

            async def run(self, message):
                self.calls += 1
                if self.calls < 2:
                    raise RuntimeError("model unavailable")
                return "gotovo"

        monkeypatch.setattr(runner_utils, "RunnerHelper", _Helper)
        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        await scheduler._execute_job(_job())

        assert len(built) == 1, "a helper per attempt, and a client with it"
        # And the second attempt sees what the first did, because it is the
        # same session.
        assert built[0]["session_id"].startswith("scheduler-test-job-")
        assert built[0]["session_service"] is not None

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_the_session_writes(self, scheduler):
        closed = {"value": False}

        class _Service:
            async def close(self):
                closed["value"] = True

        scheduler._adk_session_service = _Service()

        await scheduler.stop()

        assert closed["value"] is True
