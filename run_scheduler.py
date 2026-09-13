"""
Scheduler Runner for Google Workspace ADK Multi-Agent System

Entry point for running the scheduler with CLI management.

Usage:
    python run_scheduler.py              # Start scheduler with CLI
    python run_scheduler.py --no-cli     # Start scheduler without CLI (daemon mode)

CLI Commands:
    add      - Add a new scheduled job (interactive)
    list     - List all scheduled jobs
    remove   - Remove a job by ID
    pause    - Pause a job by ID
    resume   - Resume a paused job by ID
    status   - Show scheduler status
    help     - Show available commands
    exit     - Stop scheduler and exit
"""

import os
import sys
import asyncio
import logging
import time
import uuid

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

# Set interface context so HITL logic knows not to use blocking terminal input
os.environ.setdefault('HITL_INTERFACE', 'scheduler')

from interfaces.scheduler_interface import SchedulerInterface
from utils.process_guard import (
    hard_exit,
    notify_ready,
    notify_watchdog,
    watchdog_interval_seconds,
)
from config.scheduler_config import ScheduledJob, JobTrigger
from main import sanitize_emojis


def print_banner():
    """Print scheduler banner."""
    print("=" * 60)
    print("  Google Workspace ADK - Scheduler")
    print("  Recurring task execution via Smart Orchestrator")
    print("=" * 60)


def print_help():
    """Print available commands."""
    print("\n=== Scheduler Commands ===")
    print("  add      - Add a new scheduled job")
    print("  list     - List all scheduled jobs")
    print("  remove   - Remove a job by ID")
    print("  pause    - Pause a job by ID")
    print("  resume   - Resume a paused job by ID")
    print("  status   - Show scheduler status")
    print("  help     - Show this help")
    print("  exit     - Stop scheduler and exit\n")


def create_job_interactive() -> ScheduledJob:
    """Interactive job creation via CLI prompts."""
    print("\n--- Add New Scheduled Job ---")

    # Job ID
    default_id = f"job-{uuid.uuid4().hex[:6]}"
    job_id = input(f"Job ID [{default_id}]: ").strip() or default_id

    # Job name
    name = input("Job name: ").strip()
    if not name:
        raise ValueError("Job name is required")

    # Agent request
    print("Agent request (natural language - same as you'd type in CLI):")
    agent_request = input("> ").strip()
    if not agent_request:
        raise ValueError("Agent request is required")

    # Trigger type
    print("\nTrigger type:")
    print("  1. cron     - Schedule with cron expression (e.g., weekdays at 9am)")
    print("  2. interval - Repeat every N seconds")
    print("  3. date     - Run once at specific date/time")
    trigger_choice = input("Choose [1/2/3]: ").strip()

    trigger = None
    if trigger_choice == "1":
        print("\nCron format: minute hour day month day_of_week")
        print("Examples:")
        print("  0 9 * * MON-FRI   = weekdays at 9:00")
        print("  0 8 * * MON       = every Monday at 8:00")
        print("  */30 * * * *      = every 30 minutes")
        print("  0 0 1 * *         = first day of month at midnight")
        cron = input("Cron expression: ").strip()
        if not cron:
            raise ValueError("Cron expression is required")
        trigger = JobTrigger(type="cron", cron_expression=cron)

    elif trigger_choice == "2":
        seconds = input("Interval in seconds: ").strip()
        if not seconds.isdigit():
            raise ValueError("Interval must be a number")
        trigger = JobTrigger(type="interval", interval_seconds=int(seconds))

    elif trigger_choice == "3":
        print("Date format: YYYY-MM-DD HH:MM:SS")
        run_date = input("Run date: ").strip()
        if not run_date:
            raise ValueError("Run date is required")
        trigger = JobTrigger(type="date", run_date=run_date)
    else:
        raise ValueError(f"Invalid trigger choice: {trigger_choice}")

    # Timezone
    tz = input("Timezone [Europe/Zagreb]: ").strip() or "Europe/Zagreb"
    trigger.timezone = tz

    # Max retries
    retries = input("Max retries [2]: ").strip() or "2"

    # Description
    description = input("Description (optional): ").strip() or None

    return ScheduledJob(
        id=job_id,
        name=name,
        agent_request=agent_request,
        trigger=trigger,
        enabled=True,
        max_retries=int(retries),
        description=description
    )


def print_jobs_table(jobs: list):
    """Print jobs in a formatted table."""
    if not jobs:
        print("\nNo scheduled jobs.")
        return

    print(f"\n{'ID':<20} {'Name':<25} {'Trigger':<25} {'Enabled':<8} {'Last Status':<12} {'Next Run'}")
    print("-" * 120)
    for j in jobs:
        enabled = "Yes" if j["enabled"] else "PAUSED"
        print(f"{j['id']:<20} {j['name']:<25} {j['trigger']:<25} {enabled:<8} {j['last_status']:<12} {j['next_run']}")

    print(f"\nTotal: {len(jobs)} jobs")


async def run_cli(scheduler: SchedulerInterface):
    """Run interactive CLI for managing scheduled jobs."""
    print_help()

    loop = asyncio.get_event_loop()

    while True:
        try:
            # Use run_in_executor for non-blocking input
            cmd = await loop.run_in_executor(None, lambda: input("\nScheduler> ").strip().lower())

            if not cmd:
                continue

            if cmd == "exit" or cmd == "quit":
                print("Stopping scheduler...")
                await scheduler.stop()
                break

            elif cmd == "help":
                print_help()

            elif cmd == "add":
                try:
                    job = await loop.run_in_executor(None, create_job_interactive)
                    scheduler.add_job(job)
                    print(f"\n[OK] Job '{job.id}' added: {job.name}")
                    print(f"     Trigger: {job.trigger.type} ({job.trigger.cron_expression or f'{job.trigger.interval_seconds}s' or job.trigger.run_date})")
                    print(f"     Request: {job.agent_request}")
                except ValueError as e:
                    print(f"[ERROR] {e}")
                except KeyboardInterrupt:
                    print("\nCancelled.")

            elif cmd == "list":
                jobs = scheduler.list_jobs()
                print_jobs_table(jobs)

            elif cmd == "remove":
                job_id = await loop.run_in_executor(None, lambda: input("Job ID to remove: ").strip())
                if scheduler.remove_job(job_id):
                    print(f"[OK] Job '{job_id}' removed")
                else:
                    print(f"[ERROR] Job '{job_id}' not found")

            elif cmd == "pause":
                job_id = await loop.run_in_executor(None, lambda: input("Job ID to pause: ").strip())
                if scheduler.pause_job(job_id):
                    print(f"[OK] Job '{job_id}' paused")
                else:
                    print(f"[ERROR] Failed to pause '{job_id}'")

            elif cmd == "resume":
                job_id = await loop.run_in_executor(None, lambda: input("Job ID to resume: ").strip())
                if scheduler.resume_job(job_id):
                    print(f"[OK] Job '{job_id}' resumed")
                else:
                    print(f"[ERROR] Failed to resume '{job_id}'")

            elif cmd == "status":
                ap_jobs = scheduler.scheduler.get_jobs()
                print(f"\nScheduler running: {scheduler.scheduler.running}")
                print(f"Registered jobs: {len(scheduler.config.jobs)}")
                print(f"Active APScheduler jobs: {len(ap_jobs)}")
                print(f"Jobs with results: {len(scheduler.job_results)}")

                if scheduler.job_results:
                    print("\nRecent results:")
                    for job_id, result in scheduler.job_results.items():
                        print(f"  {job_id}: {result['status']} at {result['timestamp']} ({result['elapsed']}s)")

            else:
                print(f"Unknown command: '{cmd}'. Type 'help' for available commands.")

        except KeyboardInterrupt:
            print("\nStopping scheduler...")
            await scheduler.stop()
            break
        except EOFError:
            break


async def supervise_scheduler(scheduler) -> None:
    """Idle loop for daemon mode that also proves the scheduler is still alive.

    A bare `await asyncio.sleep(1)` loop keeps the PID up even after the
    APScheduler underneath has shut down, which systemd happily reports as
    "active (running)". Raising here reaches the fatal handler, which exits
    hard so Restart=always can do its job.
    """
    heartbeat_every = watchdog_interval_seconds()
    check_every = float(os.getenv("SCHEDULER_HEALTHCHECK_INTERVAL_SECONDS", "30"))
    tick = min([v for v in (check_every, heartbeat_every) if v and v > 0] or [30.0])
    since_check = since_heartbeat = 0.0

    while True:
        await asyncio.sleep(tick)
        since_check += tick
        since_heartbeat += tick

        if since_check >= check_every:
            since_check = 0.0
            ap = getattr(scheduler, "scheduler", None)
            if ap is not None and not getattr(ap, "running", False):
                raise RuntimeError("APScheduler stopped running")

        if heartbeat_every and since_heartbeat >= heartbeat_every:
            since_heartbeat = 0.0
            notify_watchdog("scheduler running")


async def main():
    """Main entry point."""
    print_banner()

    daemon_mode = "--no-cli" in sys.argv

    scheduler = SchedulerInterface()

    try:
        # Start scheduler (initializes agent system + loads saved jobs)
        await scheduler.start()

        jobs = scheduler.list_jobs()
        if jobs:
            print(f"\nLoaded {len(jobs)} saved job(s)")
            print_jobs_table(jobs)

        if daemon_mode:
            print("\nRunning in daemon mode (no CLI). Press Ctrl+C to stop.")
            notify_ready("scheduler running")
            await supervise_scheduler(scheduler)
        else:
            await run_cli(scheduler)

    except KeyboardInterrupt:
        print("\nShutting down...")
        await scheduler.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await asyncio.wait_for(scheduler.stop(), timeout=15)
        except Exception as stop_exc:
            # Never let cleanup replace the error that actually killed us.
            logger.warning(f"scheduler.stop() failed during shutdown (ignored): {stop_exc}")
        # hard_exit, not sys.exit: non-daemon threads (Cloud Logging) would
        # otherwise keep this process alive and systemd would never restart it.
        hard_exit(1, f"fatal error: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete")
