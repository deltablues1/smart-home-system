"""
Scheduler ADK Tools

ADK-compatible tools for managing scheduled/recurring agent tasks.
Uses the SchedulerInterface from interfaces/scheduler_interface.py.

Tools:
- scheduler_add_job: Add a new scheduled job
- scheduler_list_jobs: List all scheduled jobs
- scheduler_remove_job: Remove a scheduled job
- scheduler_pause_job: Pause a job
- scheduler_resume_job: Resume a paused job
"""

import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton scheduler instance. Registered ONLY by the CLI path
# (main.py initialize_agents(start_scheduler=True)); web/RPi deployments run
# jobs in a separate process (run_scheduler.py), so there these tools have no
# instance and must say so honestly instead of "restart the system".
_scheduler_instance = None

_SCHEDULER_UNAVAILABLE = (
    "Scheduler is not running in this process. Recurring jobs are managed by "
    "the CLI (python main.py) or the scheduler daemon (python run_scheduler.py) "
    "— tell the user to add/manage the job there."
)

# Chat that should receive the result of jobs created in this turn. The
# Telegram interface sets it per message; the scheduler daemon reads it back
# off the saved job so the answer lands where the user asked for it.
_delivery_target: Optional[str] = None


def set_delivery_target(chat_id: Optional[str]) -> None:
    """Remember which chat is talking to us right now."""
    global _delivery_target
    _delivery_target = str(chat_id) if chat_id else None


def _store_available() -> bool:
    """True when jobs can be persisted for the scheduler daemon to pick up.

    The Telegram/voice processes deliberately do not run APScheduler (that is
    the daemon's job), but they can still write to the shared job file — which
    is what makes "napravi X u 21h" work from a chat message.
    """
    return os.getenv("SCHEDULER_STORE_FALLBACK", "true").lower() != "false"


def _load_store():
    from config.scheduler_config import load_jobs
    return load_jobs()


def _save_store(config) -> None:
    from config.scheduler_config import save_jobs
    save_jobs(config)


def _store_job_view(job) -> dict:
    from interfaces.scheduler_interface import SchedulerInterface
    return {
        "id": job.id,
        "name": job.name,
        "agent_request": job.agent_request,
        "trigger": f"{job.trigger.type}: {SchedulerInterface._describe_trigger(job.trigger)}",
        "enabled": job.enabled,
    }


def set_scheduler_instance(scheduler):
    """Set the global scheduler instance (called by main.py during init)."""
    global _scheduler_instance
    _scheduler_instance = scheduler
    logger.info("Scheduler instance registered with ADK tools")


def get_scheduler_instance():
    """Get the global scheduler instance."""
    return _scheduler_instance


def _add_job_to_store(
    job_id: str,
    name: str,
    agent_request: str,
    trigger_type: str,
    cron_expression: Optional[str],
    interval_seconds: Optional[int],
    run_date: Optional[str],
    timezone: str,
    max_retries: int,
) -> dict:
    """Persist a job for the scheduler daemon when this process has no scheduler.

    Telegram and the voice loop run without APScheduler on purpose (one
    executor, no double runs), so a job asked for in chat is written to the
    shared file and the adk-scheduler daemon picks it up on its next sync.
    """
    if not _store_available():
        return {"error": _SCHEDULER_UNAVAILABLE}

    from config.scheduler_config import JobTrigger, ScheduledJob

    try:
        trigger = JobTrigger(
            type=trigger_type,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            run_date=run_date,
            timezone=timezone,
        )
        _validate_trigger(trigger)

        job = ScheduledJob(
            id=job_id,
            name=name,
            agent_request=agent_request,
            trigger=trigger,
            enabled=True,
            max_retries=max_retries,
            deliver_chat_id=_delivery_target,
        )

        config = _load_store()
        config.jobs = [j for j in config.jobs if j.id != job_id] + [job]
        _save_store(config)
    except Exception as e:
        logger.error(f"Failed to store job: {e}")
        return {"error": str(e)}

    from interfaces.scheduler_interface import SchedulerInterface

    logger.info(f"[SCHEDULER-STORE] Saved job '{job_id}' for the daemon: {name}")
    return {
        "status": "success",
        "job_id": job_id,
        "name": name,
        "agent_request": agent_request,
        "trigger": f"{trigger_type}: {SchedulerInterface._describe_trigger(trigger)}",
        "timezone": timezone,
        "executor": "adk-scheduler daemon",
        "message": (
            f"Zadatak '{name}' je zapisan i preuzet će ga scheduler servis "
            f"(unutar {_sync_interval()} s). Rezultat stiže u ovaj chat."
        ),
    }


def _validate_trigger(trigger) -> None:
    """Reject a trigger that can never fire instead of saving a dead job."""
    if trigger.type == "cron" and not trigger.cron_expression:
        raise ValueError("cron trigger traži cron_expression")
    if trigger.type == "interval" and not trigger.interval_seconds:
        raise ValueError("interval trigger traži interval_seconds")
    if trigger.type == "date":
        if not trigger.run_date:
            raise ValueError("date trigger traži run_date")
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(trigger.timezone)
        except Exception:
            tz = None
        parsed = datetime.fromisoformat(str(trigger.run_date))
        if tz is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        if parsed <= now:
            raise ValueError(
                f"vrijeme {trigger.run_date} je u prošlosti — traži od korisnika točan termin"
            )
    if trigger.type not in ("cron", "interval", "date"):
        raise ValueError(f"nepoznat trigger_type '{trigger.type}'")


def _sync_interval() -> int:
    try:
        return int(os.getenv("SCHEDULER_SYNC_INTERVAL_SECONDS", "30"))
    except ValueError:
        return 30


def _set_store_job(job_id: str, enabled: Optional[bool] = None, remove: bool = False) -> dict:
    """Enable/disable/remove a stored job (daemon picks the change up on sync)."""
    if not _store_available():
        return {"error": _SCHEDULER_UNAVAILABLE}

    config = _load_store()
    match = next((j for j in config.jobs if j.id == job_id), None)
    if match is None:
        return {"error": f"Job '{job_id}' not found."}

    if remove:
        config.jobs = [j for j in config.jobs if j.id != job_id]
        action = "obrisan"
    else:
        match.enabled = bool(enabled)
        action = "nastavljen" if enabled else "pauziran"

    _save_store(config)
    return {
        "status": "success",
        "message": f"Zadatak '{match.name}' je {action} (vrijedi za {_sync_interval()} s).",
    }


async def scheduler_add_job(
    name: str,
    agent_request: str,
    trigger_type: str,
    cron_expression: Optional[str] = None,
    interval_seconds: Optional[int] = None,
    run_date: Optional[str] = None,
    timezone: str = "Europe/Zagreb",
    max_retries: int = 2,
    job_id: Optional[str] = None
) -> dict:
    """
    Add a new scheduled job that executes an agent request on a schedule.

    The agent_request is natural language - the same text a user would type in the CLI.
    The Smart Orchestrator will route it to the appropriate agent when triggered.

    Args:
        name: Human-readable job name (e.g., "Weekly Sales Report")
        agent_request: Natural language request for agents (e.g., "Send weekly sales report to team@company.com")
        trigger_type: Type of trigger - "cron", "interval", or "date"
        cron_expression: Cron expression for cron triggers (format: "minute hour day month day_of_week")
                        Examples: "0 9 * * MON-FRI" (weekdays 9am), "0 8 * * MON" (Mondays 8am),
                        "*/30 * * * *" (every 30 min), "0 0 1 * *" (1st of month midnight)
        interval_seconds: Interval in seconds for interval triggers (e.g., 3600 for hourly)
        run_date: ISO datetime for one-time date triggers (e.g., "2026-03-10 14:00:00")
        timezone: Timezone for the schedule (default: Europe/Zagreb)
        max_retries: Max retry attempts on failure (default: 2)
        job_id: Optional custom job ID (auto-generated if not provided)

    Returns:
        Dictionary with job details and confirmation
    """
    scheduler = get_scheduler_instance()
    from config.scheduler_config import ScheduledJob, JobTrigger

    if not job_id:
        job_id = f"job-{uuid.uuid4().hex[:8]}"

    if scheduler is None:
        return _add_job_to_store(
            job_id=job_id,
            name=name,
            agent_request=agent_request,
            trigger_type=trigger_type,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            run_date=run_date,
            timezone=timezone,
            max_retries=max_retries,
        )

    try:
        trigger = JobTrigger(
            type=trigger_type,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            run_date=run_date,
            timezone=timezone
        )

        job = ScheduledJob(
            id=job_id,
            name=name,
            agent_request=agent_request,
            trigger=trigger,
            enabled=True,
            max_retries=max_retries
        )

        scheduler.add_job(job)

        # Get next run time
        ap_job = scheduler.scheduler.get_job(job_id)
        next_run = str(ap_job.next_run_time) if ap_job and ap_job.next_run_time else "N/A"

        trigger_desc = cron_expression or f"every {interval_seconds}s" or run_date
        return {
            "status": "success",
            "job_id": job_id,
            "name": name,
            "agent_request": agent_request,
            "trigger": f"{trigger_type}: {trigger_desc}",
            "timezone": timezone,
            "next_run": next_run,
            "message": f"Job '{name}' scheduled successfully. Next run: {next_run}"
        }

    except Exception as e:
        logger.error(f"Failed to add job: {e}")
        return {"error": str(e)}


async def scheduler_list_jobs() -> dict:
    """
    List all scheduled jobs with their current status.

    Returns:
        Dictionary with list of all jobs, their triggers, next run times, and last execution status.
    """
    scheduler = get_scheduler_instance()
    if scheduler is None:
        if not _store_available():
            return {"error": _SCHEDULER_UNAVAILABLE, "jobs": []}
        jobs = [_store_job_view(j) for j in _load_store().jobs]
        return {
            "total_jobs": len(jobs),
            "jobs": jobs,
            "scheduler_running": False,
            "note": "Popis iz zajedničke datoteke; izvršava ih adk-scheduler servis.",
        }

    jobs = scheduler.list_jobs()
    return {
        "total_jobs": len(jobs),
        "jobs": jobs,
        "scheduler_running": scheduler.scheduler.running
    }


async def scheduler_remove_job(job_id: str) -> dict:
    """
    Remove a scheduled job by its ID.

    Args:
        job_id: The ID of the job to remove

    Returns:
        Dictionary with removal confirmation
    """
    scheduler = get_scheduler_instance()
    if scheduler is None:
        return _set_store_job(job_id, remove=True)

    if scheduler.remove_job(job_id):
        return {"status": "success", "message": f"Job '{job_id}' removed successfully."}
    else:
        return {"error": f"Job '{job_id}' not found."}


async def scheduler_pause_job(job_id: str) -> dict:
    """
    Pause a scheduled job. The job will not execute until resumed.

    Args:
        job_id: The ID of the job to pause

    Returns:
        Dictionary with pause confirmation
    """
    scheduler = get_scheduler_instance()
    if scheduler is None:
        return _set_store_job(job_id, enabled=False)

    if scheduler.pause_job(job_id):
        return {"status": "success", "message": f"Job '{job_id}' paused."}
    else:
        return {"error": f"Failed to pause job '{job_id}'."}


async def scheduler_resume_job(job_id: str) -> dict:
    """
    Resume a paused scheduled job.

    Args:
        job_id: The ID of the job to resume

    Returns:
        Dictionary with resume confirmation
    """
    scheduler = get_scheduler_instance()
    if scheduler is None:
        return _set_store_job(job_id, enabled=True)

    if scheduler.resume_job(job_id):
        return {"status": "success", "message": f"Job '{job_id}' resumed."}
    else:
        return {"error": f"Failed to resume job '{job_id}'."}


def get_scheduler_adk_tools() -> list:
    """
    Get all scheduler ADK tools as a list of async functions.

    Returns:
        List of tool functions for use with ADK agents
    """
    tools = [
        scheduler_add_job,
        scheduler_list_jobs,
        scheduler_remove_job,
        scheduler_pause_job,
        scheduler_resume_job
    ]
    logger.info(f"Scheduler ADK tools loaded: {len(tools)} tools")
    return tools
