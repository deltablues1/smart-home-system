"""
Scheduler Configuration

Pydantic models for scheduled jobs with JSON file persistence.
Jobs are stored in config/scheduled_jobs.json.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

def _resolve_jobs_file() -> Path:
    """Select scheduler job file based on deployment profile when available."""
    base_dir = Path(__file__).parent
    profile = os.getenv("DEPLOYMENT_PROFILE", "full").strip().lower()
    if profile:
        profile_path = base_dir / f"scheduled_jobs.{profile}.json"
        if profile_path.exists():
            return profile_path
    return base_dir / "scheduled_jobs.json"


# Default path for job persistence
JOBS_FILE = _resolve_jobs_file()

def _resolve_results_file() -> Path:
    """Where run outcomes are recorded.

    data/, not config/: the jobs themselves are configuration a person wrote,
    their outcomes are runtime state the machine wrote. It used to live only
    in memory, so after a restart nobody could tell whether last night's job
    had run, failed, or stopped half way.
    """
    return Path(
        os.getenv("SCHEDULER_RESULTS_FILE", "")
        or Path(__file__).resolve().parents[1] / "data" / "scheduled_jobs_results.json"
    )


RESULTS_FILE = _resolve_results_file()


class JobTrigger(BaseModel):
    """Trigger configuration for a scheduled job."""
    type: str = Field(description="Trigger type: 'cron', 'interval', or 'date'")

    # Cron fields (type='cron')
    cron_expression: Optional[str] = Field(
        None,
        description="Cron expression: 'minute hour day month day_of_week' (e.g., '0 9 * * MON-FRI')"
    )

    # Interval fields (type='interval')
    interval_seconds: Optional[int] = Field(None, description="Interval in seconds")

    # Date fields (type='date')
    run_date: Optional[str] = Field(None, description="ISO format date for one-time execution")

    # Common
    timezone: str = Field("Europe/Zagreb", description="Timezone for the trigger")


class ScheduledJob(BaseModel):
    """A scheduled job that triggers an agent request."""
    id: str = Field(description="Unique job identifier")
    name: str = Field(description="Human-readable job name")
    agent_request: str = Field(description="Natural language request for the agent system")
    trigger: JobTrigger
    enabled: bool = Field(True, description="Whether the job is active")
    max_retries: int = Field(2, description="Max retry attempts on failure")
    description: Optional[str] = Field(None, description="Optional description")
    # "nl" (default) runs agent_request through the orchestrator;
    # "briefing" calls the deterministic daily-briefing service directly and
    # delivers the result to the authorized Telegram chat.
    action_type: str = Field("nl", description="Job action: 'nl' or 'briefing'")
    # Chat that asked for the job, so the daemon can answer where the user is
    # standing. Empty falls back to TELEGRAM_CHAT_ID.
    deliver_chat_id: Optional[str] = Field(
        None, description="Telegram chat id that should receive the result"
    )


class SchedulerConfig(BaseModel):
    """Root configuration containing all scheduled jobs."""
    jobs: List[ScheduledJob] = Field(default_factory=list)


def load_jobs(file_path: Path = JOBS_FILE) -> SchedulerConfig:
    """Load scheduled jobs from JSON file."""
    if not file_path.exists():
        logger.info(f"No jobs file found at {file_path}, starting with empty config")
        return SchedulerConfig()

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        config = SchedulerConfig(**data)
        logger.info(f"Loaded {len(config.jobs)} scheduled jobs from {file_path}")
        return config
    except Exception as e:
        logger.error(f"Failed to load jobs from {file_path}: {e}")
        return SchedulerConfig()


def load_results(file_path: Optional[Path] = None) -> dict:
    """Last recorded outcome per job id. Empty when there is nothing yet.

    The path is resolved on the call, not baked into the default argument:
    a default is bound when the function is defined, so redirecting the
    location afterwards — a test, a second deployment — had no effect and the
    writes went to the real file anyway.
    """
    file_path = file_path or _resolve_results_file()
    if not file_path.exists():
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Failed to load job results from {file_path}: {e}")
        return {}


def save_results(results: dict, file_path: Optional[Path] = None) -> None:
    """Write the outcomes atomically, same reasoning as save_jobs.

    Path resolved per call, for the reason in load_results.
    """
    file_path = file_path or _resolve_results_file()
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)
    except Exception as e:
        logger.error(f"Failed to save job results to {file_path}: {e}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def save_jobs(config: SchedulerConfig, file_path: Path = JOBS_FILE) -> None:
    """Save scheduled jobs to JSON file (atomic replace).

    Writing straight to the target file can leave it truncated/corrupted if
    the process dies mid-write or two processes save at once; os.replace of a
    fully written temp file is atomic on both POSIX and Windows.
    """
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config.model_dump(), f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, file_path)
        logger.info(f"Saved {len(config.jobs)} jobs to {file_path}")
    except Exception as e:
        logger.error(f"Failed to save jobs to {file_path}: {e}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
