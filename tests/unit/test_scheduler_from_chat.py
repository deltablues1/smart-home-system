"""Tests for scheduling a job from a chat message.

The Telegram and voice processes run no APScheduler on purpose — one executor
means a job can never fire twice — so "napravi X u 21h" has to travel:
chat -> shared job file -> adk-scheduler daemon -> result back to that chat.
Each leg of that trip is pinned down here.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.scheduler_config import JobTrigger, ScheduledJob, SchedulerConfig  # noqa: E402
from tools.adk_tools import scheduler_adk_tools as sched  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the job store to a temp file for the whole tool module."""
    path = tmp_path / "scheduled_jobs.json"
    state = SchedulerConfig()

    def fake_load():
        return state.model_copy(deep=True)

    def fake_save(config):
        nonlocal state
        state = config.model_copy(deep=True)
        path.write_text(config.model_dump_json(), encoding="utf-8")

    monkeypatch.setattr(sched, "_load_store", fake_load)
    monkeypatch.setattr(sched, "_save_store", fake_save)
    monkeypatch.setattr(sched, "_scheduler_instance", None)
    sched.set_delivery_target(None)
    return lambda: state


def _tomorrow_at(hour=21):
    return (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M:%S")


def test_job_asked_for_in_chat_is_persisted_for_the_daemon(store):
    sched.set_delivery_target("123456789")

    result = asyncio.run(sched.scheduler_add_job(
        name="Zalij cvijeće",
        agent_request="Podsjeti me da zalijem cvijeće",
        trigger_type="date",
        run_date=_tomorrow_at(21),
    ))

    assert result["status"] == "success"
    assert result["executor"] == "adk-scheduler daemon"

    jobs = store().jobs
    assert len(jobs) == 1
    assert jobs[0].agent_request == "Podsjeti me da zalijem cvijeće"
    assert jobs[0].deliver_chat_id == "123456789"   # answers where it was asked


def test_a_time_already_past_is_rejected_instead_of_saved(store):
    result = asyncio.run(sched.scheduler_add_job(
        name="Prošlost",
        agent_request="ne bi se trebalo izvršiti",
        trigger_type="date",
        run_date="2020-01-01 21:00:00",
    ))

    assert "error" in result
    assert "prošlosti" in result["error"]
    assert store().jobs == []


def test_incomplete_trigger_is_rejected(store):
    result = asyncio.run(sched.scheduler_add_job(
        name="Bez izraza",
        agent_request="nešto",
        trigger_type="cron",
    ))

    assert "error" in result
    assert store().jobs == []


def test_listing_reads_the_shared_store(store):
    asyncio.run(sched.scheduler_add_job(
        name="Jutarnji pregled",
        agent_request="daj mi pregled dana",
        trigger_type="cron",
        cron_expression="0 8 * * *",
    ))

    listed = asyncio.run(sched.scheduler_list_jobs())

    assert listed["total_jobs"] == 1
    assert listed["scheduler_running"] is False
    assert "cron: 0 8 * * *" in listed["jobs"][0]["trigger"]


def test_pause_resume_and_remove_go_through_the_store(store):
    created = asyncio.run(sched.scheduler_add_job(
        name="Test",
        agent_request="nešto",
        trigger_type="interval",
        interval_seconds=3600,
    ))
    job_id = created["job_id"]

    assert "success" in asyncio.run(sched.scheduler_pause_job(job_id))["status"]
    assert store().jobs[0].enabled is False

    assert "success" in asyncio.run(sched.scheduler_resume_job(job_id))["status"]
    assert store().jobs[0].enabled is True

    assert "success" in asyncio.run(sched.scheduler_remove_job(job_id))["status"]
    assert store().jobs == []


def test_unknown_job_is_reported_not_silently_ignored(store):
    assert "error" in asyncio.run(sched.scheduler_remove_job("job-nepostojeci"))


def test_fallback_can_be_switched_off(store, monkeypatch):
    monkeypatch.setenv("SCHEDULER_STORE_FALLBACK", "false")

    result = asyncio.run(sched.scheduler_add_job(
        name="Test", agent_request="x", trigger_type="interval", interval_seconds=60,
    ))

    assert "error" in result
    assert "not running in this process" in result["error"]


# --- daemon side -----------------------------------------------------------

def _interface():
    from interfaces.scheduler_interface import SchedulerInterface

    iface = SchedulerInterface.__new__(SchedulerInterface)
    iface.scheduler = MagicMock()
    iface.config = SchedulerConfig()
    iface.job_results = {}
    # __init__ is skipped here on purpose, so anything a run reads has to be
    # set by hand. One session service for the process, built lazily.
    iface._adk_session_service = None
    return iface


def _job(job_id="job-1", enabled=True, trigger_type="interval"):
    return ScheduledJob(
        id=job_id,
        name="Test job",
        agent_request="napravi nešto",
        trigger=JobTrigger(type=trigger_type, interval_seconds=3600),
        enabled=enabled,
    )


def test_daemon_adopts_a_job_written_by_another_process():
    iface = _interface()
    stored = SchedulerConfig(jobs=[_job()])

    with patch("interfaces.scheduler_interface.load_jobs", return_value=stored):
        outcome = iface.sync_from_store()

    assert outcome["added"] == 1
    iface.scheduler.add_job.assert_called_once()
    assert iface.config.jobs[0].id == "job-1"


def test_daemon_unschedules_a_job_deleted_from_the_store():
    iface = _interface()
    iface.config = SchedulerConfig(jobs=[_job()])

    with patch("interfaces.scheduler_interface.load_jobs", return_value=SchedulerConfig()):
        outcome = iface.sync_from_store()

    assert outcome["removed"] == 1
    iface.scheduler.remove_job.assert_called_once_with("job-1")


def test_disabled_job_is_removed_from_the_scheduler_not_run():
    iface = _interface()
    stored = SchedulerConfig(jobs=[_job(enabled=False)])

    with patch("interfaces.scheduler_interface.load_jobs", return_value=stored):
        iface.sync_from_store()

    iface.scheduler.add_job.assert_not_called()
    iface.scheduler.remove_job.assert_called_once_with("job-1")


def test_unchanged_job_is_not_rescheduled_every_sync():
    iface = _interface()
    job = _job()
    iface.config = SchedulerConfig(jobs=[job])
    stored = SchedulerConfig(jobs=[job.model_copy(deep=True)])

    with patch("interfaces.scheduler_interface.load_jobs", return_value=stored):
        outcome = iface.sync_from_store()

    assert outcome == {"added": 0, "removed": 0}
    iface.scheduler.add_job.assert_not_called()


def test_unreadable_store_keeps_the_running_jobs():
    iface = _interface()
    iface.config = SchedulerConfig(jobs=[_job()])

    with patch("interfaces.scheduler_interface.load_jobs", side_effect=OSError("disk gone")):
        outcome = iface.sync_from_store()

    assert outcome == {"added": 0, "removed": 0}
    assert iface.config.jobs[0].id == "job-1"        # not wiped by a bad read
    iface.scheduler.remove_job.assert_not_called()


def test_job_result_is_delivered_to_the_chat_that_asked():
    iface = _interface()
    job = ScheduledJob(
        id="job-2",
        name="Zalij cvijeće",
        agent_request="podsjeti me",
        trigger=JobTrigger(type="date", run_date="2026-08-21 21:00:00"),
        deliver_chat_id="123456789",
    )

    helper = MagicMock()
    helper.run = AsyncMock(return_value="Gotovo, cvijeće zaliveno.")
    iface.system = MagicMock()
    iface._deliver_to_telegram = AsyncMock(return_value=True)

    with patch("agents.adk_agents.runner_utils.RunnerHelper", return_value=helper):
        with patch("config.scheduler_config.save_jobs"):
            asyncio.run(iface._execute_job(job))

    text, chat_id = iface._deliver_to_telegram.await_args.args
    assert chat_id == "123456789"
    assert "Gotovo, cvijeće zaliveno." in text
    assert iface.job_results["job-2"]["delivered"] is True


def test_one_shot_job_is_dropped_from_the_store_after_it_runs():
    iface = _interface()
    job = ScheduledJob(
        id="job-3",
        name="Jednokratno",
        agent_request="x",
        trigger=JobTrigger(type="date", run_date="2026-08-21 21:00:00"),
    )
    iface.config = SchedulerConfig(jobs=[job])

    with patch("interfaces.scheduler_interface.save_jobs") as saved:
        iface._forget_one_shot(job)

    assert iface.config.jobs == []
    saved.assert_called_once()


def test_recurring_job_survives_execution():
    iface = _interface()
    job = _job(trigger_type="interval")
    iface.config = SchedulerConfig(jobs=[job])

    with patch("interfaces.scheduler_interface.save_jobs") as saved:
        iface._forget_one_shot(job)

    assert iface.config.jobs == [job]
    saved.assert_not_called()


def test_delivery_falls_back_to_the_configured_chat():
    iface = _interface()
    sent = {}

    class FakeBot:
        def __init__(self, token):
            sent["token"] = token

        async def send_message(self, chat_id, text):
            sent["chat_id"] = chat_id
            sent["text"] = text

    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "730437"}):
        with patch("telegram.Bot", FakeBot):
            delivered = asyncio.run(iface._deliver_to_telegram("poruka"))

    assert delivered is True
    assert sent["chat_id"] == "730437"


def test_delivery_without_configuration_reports_failure():
    iface = _interface()
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
        assert asyncio.run(iface._deliver_to_telegram("poruka")) is False
