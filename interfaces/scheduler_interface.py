"""
Scheduler Interface for Google Workspace ADK System

Provides APScheduler-based recurring/scheduled task execution.
Jobs are defined as natural language agent requests and executed
through the Smart Orchestrator pipeline.

Usage:
    python run_scheduler.py
"""

import os
import logging
import asyncio
import time
import uuid
from typing import Optional, Dict, List

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

from .base_interface import BaseInterface
from config.scheduler_config import (
    ScheduledJob, JobTrigger, SchedulerConfig,
    load_jobs, save_jobs, load_results, save_results
)
from services import run_effects

logger = logging.getLogger(__name__)


class SchedulerInterface(BaseInterface):
    """
    Scheduler interface that executes agent requests on a schedule.

    Extends BaseInterface to reuse the WorkspaceADKSystem and
    RunnerHelper for executing tasks through the Smart Orchestrator.
    """

    def __init__(self):
        """Initialize scheduler interface."""
        super().__init__(session_prefix="scheduler")

        self.scheduler = AsyncIOScheduler(timezone="Europe/Zagreb")
        self.config: SchedulerConfig = SchedulerConfig()
        # Last outcome per job, loaded from disk. In memory only, nobody could
        # tell after a restart whether last night's job had run at all.
        self.job_results: Dict[str, dict] = load_results()
        # One ADK session service for the life of the process. A RunnerHelper
        # builds its own when given none, so a helper per attempt meant a new
        # Firestore client per attempt, none of them ever closed.
        self._adk_session_service = None

        logger.info("SchedulerInterface initialized")

    def _get_adk_session_service(self):
        """Lazily build the one session service this process will use."""
        if self._adk_session_service is None:
            use_persistent = os.environ.get(
                "USE_PERSISTENT_ADK_SESSIONS", "false"
            ).lower() == "true"
            if use_persistent:
                from services.adk_session_service import FirestoreADKSessionService
                self._adk_session_service = FirestoreADKSessionService()
                logger.info("[SCHEDULER] using FirestoreADKSessionService (shared)")
            else:
                from google.adk.sessions import InMemorySessionService
                self._adk_session_service = InMemorySessionService()
        return self._adk_session_service

    def _record_result(self, job_id: str, result: dict) -> None:
        """Remember how a run ended, on disk as well as in memory."""
        self.job_results[job_id] = result
        try:
            save_results(self.job_results)
        except Exception:
            logger.warning("[SCHEDULER] Could not persist job results", exc_info=True)

    def _build_trigger(self, trigger: JobTrigger):
        """Build APScheduler trigger from JobTrigger config."""
        if trigger.type == "cron":
            if not trigger.cron_expression:
                raise ValueError("cron_expression required for cron trigger")
            parts = trigger.cron_expression.split()
            if len(parts) != 5:
                raise ValueError(f"Invalid cron expression: '{trigger.cron_expression}' (need 5 fields: min hour day month dow)")
            return CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone=trigger.timezone
            )
        elif trigger.type == "interval":
            if not trigger.interval_seconds:
                raise ValueError("interval_seconds required for interval trigger")
            return IntervalTrigger(
                seconds=trigger.interval_seconds,
                timezone=trigger.timezone
            )
        elif trigger.type == "date":
            if not trigger.run_date:
                raise ValueError("run_date required for date trigger")
            return DateTrigger(
                run_date=trigger.run_date,
                timezone=trigger.timezone
            )
        else:
            raise ValueError(f"Unknown trigger type: {trigger.type}")

    async def _deliver_to_telegram(self, text: str, chat_id: str = "") -> bool:
        """Push a job result to a Telegram chat, if configured.

        Defaults to TELEGRAM_CHAT_ID, but a job created from a chat carries the
        id of that chat so the answer comes back where it was asked for.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = (chat_id or "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            logger.info("[SCHEDULER] No Telegram token/chat configured for delivery")
            return False
        from telegram import Bot

        bot = Bot(token=token)
        # Telegram hard limit is 4096 chars per message
        for start in range(0, len(text), 4000):
            await bot.send_message(chat_id=chat_id, text=text[start:start + 4000])
        return True

    async def _execute_briefing_job(self, job_config: ScheduledJob) -> None:
        """Deterministic daily-briefing job — no orchestrator, no NL replay."""
        job_id = job_config.id
        start_time = time.time()
        try:
            from config.user_context import get_default_user_context
            from services.daily_briefing import get_daily_briefing

            text = await get_daily_briefing(
                get_default_user_context(channel="scheduler")
            )
            delivered = await self._deliver_to_telegram(
                text, getattr(job_config, "deliver_chat_id", "") or ""
            )
            self._record_result(job_id, {
                # A briefing nobody received is not a success.
                "status": "SUCCESS" if delivered else "SUCCESS_NOT_DELIVERED",
                "result_preview": text[:200],
                "delivered": delivered,
                "elapsed": round(time.time() - start_time, 1),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "attempt": 1,
            })
            logger.info(f"[SCHEDULER] Briefing job '{job_id}' done (delivered={delivered})")
        except Exception as e:
            self._record_result(job_id, {
                "status": "FAILED",
                "error": str(e)[:200],
                "elapsed": round(time.time() - start_time, 1),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "attempt": 1,
            })
            logger.error(f"[SCHEDULER] Briefing job '{job_id}' failed: {e}")

    async def _execute_job(self, job_config: ScheduledJob) -> None:
        """
        Execute a scheduled job through the agent pipeline.

        This is called by APScheduler when a job triggers.
        """
        job_id = job_config.id

        if getattr(job_config, "action_type", "nl") == "briefing":
            await self._execute_briefing_job(job_config)
            return

        logger.info(f"[SCHEDULER] Executing job '{job_id}': {job_config.agent_request}")

        # Nobody is in the loop at 07:30. Bind the run to its own session and
        # mark it autonomous, so a tool that needs a user's confirmation is
        # refused outright rather than left pending under the default "global"
        # session, where a "da" typed later in Telegram could arm it.
        # One id per execution. The session used to be a stable
        # "scheduler-{job_id}", so yesterday's conversation was still in
        # context when today's run started — and attempt 2 replayed into
        # attempt 1's history.
        run_id = uuid.uuid4().hex[:8]
        run_session = f"scheduler-{job_id}-{run_id}"

        from services import approvals
        approvals.set_session(run_session)
        approvals.set_autonomous(True)

        from services import run_effects

        start_time = time.time()
        attempt = 0
        max_attempts = job_config.max_retries + 1

        helper = None

        while attempt < max_attempts:
            attempt += 1
            # Each attempt starts a fresh ledger of what it did. Replaying is
            # only safe while that ledger is empty.
            run_effects.start_run()
            try:
                if self.system is None:
                    self.initialize_system()

                # One helper for the whole execution, built once: the attempts
                # of a single run share a session so a second attempt can see
                # what the first already did, and the process keeps one ADK
                # session service instead of one per attempt.
                if helper is None:
                    from agents.adk_agents.runner_utils import RunnerHelper
                    helper = RunnerHelper(
                        agent=self.system.orchestrator,
                        session_id=run_session,
                        user_id="system:scheduler",
                        app_name="agents",
                        session_service=self._get_adk_session_service(),
                    )

                result = await helper.run(job_config.agent_request)
                elapsed = time.time() - start_time

                # A scheduled job whose answer only reaches the log is useless
                # to the person who asked for it.
                delivered = False
                if result:
                    try:
                        delivered = await self._deliver_to_telegram(
                            f"[{job_config.name}]\n{result}",
                            getattr(job_config, "deliver_chat_id", "") or "",
                        )
                    except Exception as delivery_error:
                        logger.warning(
                            f"[SCHEDULER] Job '{job_id}' ran but delivery failed: {delivery_error}"
                        )

                self._forget_one_shot(job_config)

                # `helper.run()` returning without raising is not the same as
                # the work being done. Two things it cannot mean:
                #   - the gate refused because the job needed a person;
                #   - the answer never reached anyone.
                # Both used to be filed as SUCCESS.
                #
                # There is deliberately no PARTIAL here. Knowing which steps
                # finished needs step-level results, which the workflow does
                # not produce yet; inventing the status would only make the
                # dashboard look more certain than the system is.
                # Action ids are "tool:digest" — unique, but not something
                # to put on a dashboard. The tool name is the readable half.
                needed = [
                    run_effects.action_label(a)
                    for a in run_effects.confirmations_needed()
                ]
                if needed:
                    status = "NEEDS_CONFIRMATION"
                elif not delivered:
                    status = "SUCCESS_NOT_DELIVERED"
                else:
                    status = "SUCCESS"

                self._record_result(job_id, {
                    "delivered": delivered,
                    "status": status,
                    "needs_confirmation": list(dict.fromkeys(needed)),
                    "result_preview": result[:200] if result else "",
                    "tools_ran": list(dict.fromkeys(run_effects.tool_calls())),
                    "run_id": run_id,
                    "elapsed": round(elapsed, 1),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "attempt": attempt
                })

                logger.info(f"[SCHEDULER] Job '{job_id}' finished {status} in {elapsed:.1f}s (attempt {attempt})")
                logger.info(f"[SCHEDULER] Result preview: {result[:200] if result else 'empty'}")
                return

            except Exception as e:
                elapsed = time.time() - start_time
                error_str = str(e)
                logger.error(f"[SCHEDULER] Job '{job_id}' failed (attempt {attempt}/{max_attempts}): {e}")

                # A retry re-runs the ENTIRE natural-language request. If a
                # tool already ran, the agent has no memory of it and would do
                # it again — that is how one failed job sent two mails. Stop,
                # and say honestly that the outcome is not known: some of the
                # work happened, some did not, and nothing here can tell which.
                effects = run_effects.tool_calls()
                if effects:
                    logger.error(
                        "[SCHEDULER] Job '%s' will NOT be retried — %d tool(s) "
                        "already ran: %s",
                        job_id, len(effects), ", ".join(dict.fromkeys(effects)),
                    )
                    self._record_result(job_id, {
                        "status": "UNKNOWN",
                        "error": error_str[:200],
                        "tools_ran": list(dict.fromkeys(effects)),
                        "run_id": run_id,
                        "elapsed": round(elapsed, 1),
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "attempt": attempt,
                    })
                    return

                if attempt < max_attempts:
                    # Nothing happened yet, so starting over is safe.
                    # Retry with backoff for rate limits
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        wait = 60 * attempt
                        logger.info(f"[SCHEDULER] Rate limited, waiting {wait}s before retry...")
                        await asyncio.sleep(wait)
                    else:
                        await asyncio.sleep(5)
                else:
                    self._record_result(job_id, {
                        "status": "FAILED",
                        "error": error_str[:200],
                        "run_id": run_id,
                        "elapsed": round(elapsed, 1),
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "attempt": attempt
                    })

    def _schedule(self, job: ScheduledJob) -> None:
        """Register a job with APScheduler only — no persistence side effects.

        Kept separate from add_job() so the disk-sync path can (re)register jobs
        written by another process without saving the file back and racing it.
        """
        self.scheduler.add_job(
            self._execute_job,
            trigger=self._build_trigger(job.trigger),
            id=job.id,
            name=job.name,
            args=[job],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

    def _forget_one_shot(self, job: ScheduledJob) -> None:
        """Drop a fired one-time job from the store.

        APScheduler removes a date job once it runs, but the file would keep it
        forever and the next sync would try to re-register a run_date in the
        past — a job that can only misfire.
        """
        if getattr(job.trigger, "type", "") != "date":
            return
        try:
            self.config.jobs = [j for j in self.config.jobs if j.id != job.id]
            save_jobs(self.config)
            logger.info(f"[SCHEDULER] One-shot job '{job.id}' done and removed from store")
        except Exception as e:
            logger.warning(f"[SCHEDULER] Could not remove one-shot job '{job.id}': {e}")

    def sync_from_store(self) -> dict:
        """Pick up jobs written by another process (Telegram/voice).

        The bot process has no APScheduler on purpose — one executor means no
        double runs — so it persists jobs and this daemon adopts them.
        """
        try:
            stored = load_jobs()
        except Exception as e:
            logger.warning(f"[SCHEDULER] Job store unreadable, keeping current jobs: {e}")
            return {"added": 0, "removed": 0}

        current = {j.id: j for j in self.config.jobs}
        incoming = {j.id: j for j in stored.jobs}
        added = removed = 0

        for job_id, job in incoming.items():
            if current.get(job_id) == job:
                continue
            try:
                if job.enabled:
                    self._schedule(job)
                    added += 1
                    logger.info(f"[SCHEDULER] Adopted job '{job_id}': {job.name}")
                else:
                    self._drop_from_scheduler(job_id)
            except Exception as e:
                logger.error(f"[SCHEDULER] Could not adopt job '{job_id}': {e}")

        for job_id in set(current) - set(incoming):
            self._drop_from_scheduler(job_id)
            removed += 1
            logger.info(f"[SCHEDULER] Job '{job_id}' disappeared from store, unscheduled")

        self.config = stored
        return {"added": added, "removed": removed}

    def _drop_from_scheduler(self, job_id: str) -> None:
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

    def add_job(self, job: ScheduledJob) -> str:
        """
        Add a scheduled job.

        Args:
            job: ScheduledJob configuration

        Returns:
            Job ID
        """
        self._schedule(job)

        # Save to config
        existing_ids = {j.id for j in self.config.jobs}
        if job.id not in existing_ids:
            self.config.jobs.append(job)
        else:
            self.config.jobs = [j if j.id != job.id else job for j in self.config.jobs]

        save_jobs(self.config)
        logger.info(f"[SCHEDULER] Added job '{job.id}': {job.name} ({job.trigger.type})")
        return job.id

    def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        original_len = len(self.config.jobs)
        self.config.jobs = [j for j in self.config.jobs if j.id != job_id]

        if len(self.config.jobs) < original_len:
            save_jobs(self.config)
            logger.info(f"[SCHEDULER] Removed job '{job_id}'")
            return True
        return False

    def pause_job(self, job_id: str) -> bool:
        """Pause a scheduled job."""
        try:
            self.scheduler.pause_job(job_id)
            for job in self.config.jobs:
                if job.id == job_id:
                    job.enabled = False
            save_jobs(self.config)
            logger.info(f"[SCHEDULER] Paused job '{job_id}'")
            return True
        except Exception as e:
            logger.error(f"Failed to pause job '{job_id}': {e}")
            return False

    def resume_job(self, job_id: str) -> bool:
        """Resume a paused job."""
        try:
            self.scheduler.resume_job(job_id)
            for job in self.config.jobs:
                if job.id == job_id:
                    job.enabled = True
            save_jobs(self.config)
            logger.info(f"[SCHEDULER] Resumed job '{job_id}'")
            return True
        except Exception as e:
            logger.error(f"Failed to resume job '{job_id}': {e}")
            return False

    def list_jobs(self) -> List[dict]:
        """List all scheduled jobs with their status."""
        jobs_info = []
        for job in self.config.jobs:
            ap_job = self.scheduler.get_job(job.id)
            next_run = str(ap_job.next_run_time) if ap_job and ap_job.next_run_time else "N/A"
            last_result = self.job_results.get(job.id, {})

            jobs_info.append({
                "id": job.id,
                "name": job.name,
                "request": job.agent_request[:60] + "..." if len(job.agent_request) > 60 else job.agent_request,
                "trigger": f"{job.trigger.type}: {self._describe_trigger(job.trigger)}",
                "enabled": job.enabled,
                "next_run": next_run,
                "last_status": last_result.get("status", "Never run"),
                "last_run": last_result.get("timestamp", "N/A")
            })
        return jobs_info

    @staticmethod
    def _describe_trigger(trigger) -> str:
        """Human-readable trigger description.

        The old one-liner rendered date jobs as "Nones": interval_seconds is
        None so f'{None}s' -> "Nones" (truthy) and run_date never showed.
        """
        if trigger.cron_expression:
            return trigger.cron_expression
        if trigger.interval_seconds is not None:
            return f"{trigger.interval_seconds}s"
        if trigger.run_date:
            return str(trigger.run_date)
        return "?"

    def _load_saved_jobs(self) -> None:
        """Load and register jobs from persistent storage."""
        self.config = load_jobs()

        for job in self.config.jobs:
            if not job.enabled:
                logger.info(f"[SCHEDULER] Skipping disabled job '{job.id}'")
                continue

            try:
                trigger = self._build_trigger(job.trigger)
                self.scheduler.add_job(
                    self._execute_job,
                    trigger=trigger,
                    id=job.id,
                    name=job.name,
                    args=[job],
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
                logger.info(f"[SCHEDULER] Loaded job '{job.id}': {job.name}")
            except Exception as e:
                logger.error(f"[SCHEDULER] Failed to load job '{job.id}': {e}")

    async def start(self) -> None:
        """Start the scheduler interface."""
        logger.info("Starting Scheduler interface...")

        # Initialize agent system
        self.initialize_system()

        # Load saved jobs
        self._load_saved_jobs()

        # Start scheduler
        self.scheduler.start()

        # Adopt jobs created by the Telegram/voice processes.
        sync_seconds = self._sync_interval_seconds()
        if sync_seconds > 0:
            self.scheduler.add_job(
                self.sync_from_store,
                trigger="interval",
                seconds=sync_seconds,
                id="_store_sync",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(f"[SCHEDULER] Watching job store every {sync_seconds}s")

        logger.info(f"[SCHEDULER] Started with {len(self.config.jobs)} jobs")

    @staticmethod
    def _sync_interval_seconds() -> int:
        try:
            return int(os.getenv("SCHEDULER_SYNC_INTERVAL_SECONDS", "30"))
        except ValueError:
            return 30

    async def stop(self) -> None:
        """Stop the scheduler interface."""
        logger.info("Stopping Scheduler interface...")
        # Tolerate a scheduler that never started or is already down: the rest
        # of this — saving jobs, flushing session writes — still has to happen.
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        save_jobs(self.config)

        # Session writes are backgrounded, so leaving without waiting drops
        # whatever the last job was still recording.
        if self._adk_session_service is not None:
            closer = getattr(self._adk_session_service, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    logger.warning("ADK session service close failed", exc_info=True)

        logger.info("Scheduler stopped, jobs saved")

    def format_response(self, response: str) -> str:
        """Format response (passthrough for scheduler)."""
        return response
