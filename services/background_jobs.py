"""Long voice requests as jobs that outlive the turn that started them.

A research question through HA Assist takes minutes. The turn cannot wait that
long, so the run is deferred: the user is told it will arrive on the phone, and
`services/late_answer` delivers it when it lands. That much already worked.

Two things did not.

**Nothing survived a restart.** The work was an `asyncio.Task` nobody held a
record of, so a deploy or a crash during those minutes dropped a promise that
had already been spoken out loud, with no trace anywhere. A job is written down
now. It still cannot be resumed — the request is prose, not a resumable plan —
but a job left `running` by a dead process is marked `interrupted` at startup,
so the answer is "that one died", not silence.

**The next turn walked into the same conversation.** The session lock is
released as soon as the deferred ack is returned, so a follow-up a minute later
ran a second orchestrator over the same ADK session while the first was still
writing to it, interleaving two conversations into one transcript. A session
with a job running is reserved here, and every orchestrator entry point checks
it — the text path and the browser stream included, not only the voice one that
started the job.

Three things this file is careful about:

* **The reservation lives in memory; the file is its record.** A disk that
  cannot be written must not silently drop the protection as well, so
  `active_for_session` answers from the in-process table first. A failed write
  costs the record after a restart, not the safety of the run in progress.
  That table keeps finished jobs too, and for the same reason read backwards:
  if the write that recorded a job's *end* fails, the stale `running` row on
  disk must not be able to reserve a conversation that is already free.
* **A failed write is reported.** `start()` used to hand back a job id whether
  or not anything reached disk.
* **Each process owns its own notepad.** Web, Telegram, the wake word and the
  scheduler all call this. Sharing one file meant any of them starting up could
  declare another's live job "interrupted" and release a session that was still
  being written to.

The reservation is deliberately narrow: it covers orchestrator runs, which
share the ADK history. Lights, sensors, the time and the weather never touch
it, so "upali svjetlo" keeps working while a research job runs — being unable
to turn the lights on for three minutes would be a worse bug than the one this
fixes.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"

# How long a finished job stays answerable ("je li gotovo?") and how many are
# kept at all. Small on purpose: this is a notepad, not an archive.
_KEEP_SECONDS = 24 * 3600
_KEEP_MAX = 50

# Every job this process has started, running or finished. Authoritative for
# reservations in both directions: the file is how they survive a restart, not
# how they are enforced, and it can be out of date either way.
_JOBS: Dict[str, dict] = {}

# Finished jobs kept in memory before the oldest are dropped. Running ones are
# never dropped — they are the reservations.
_MEMORY_MAX = 100

# Which process's notepad this is. Set by the interface at startup.
_owner = "default"


class JobStoreError(RuntimeError):
    """The job could not be written down."""


def set_owner(name: str) -> None:
    """Name this process's notepad, so it cannot disturb another's."""
    global _owner
    _owner = (name or "default").strip() or "default"


def _store_path() -> Path:
    """Runtime state, so data/ alongside the budget ledger — not config/.

    One file per owner. The env override is what lets tests and a second
    deployment on the same machine keep their own.
    """
    override = os.getenv("BACKGROUND_JOBS_FILE", "")
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parents[1]
        / "data" / f"background_jobs.{_owner}.json"
    )


def _load() -> Dict[str, dict]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error("[JOBS] Could not read %s: %s", path, e)
        return {}


def _save(jobs: Dict[str, dict]) -> None:
    """Write the notepad. Raises rather than swallowing — the caller decides."""
    path = _store_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise JobStoreError(f"could not write {path}: {e}") from e


def _prune(jobs: Dict[str, dict]) -> Dict[str, dict]:
    now = time.time()
    keep = {
        job_id: job for job_id, job in jobs.items()
        if job.get("state") == RUNNING or now - job.get("started_at", 0) < _KEEP_SECONDS
    }
    if len(keep) > _KEEP_MAX:
        ordered = sorted(keep.items(), key=lambda kv: kv[1].get("started_at", 0))
        keep = dict(ordered[-_KEEP_MAX:])
    return keep


def start(session_id: str, user_id: str, question: str) -> str:
    """Reserve the session and record the job. Returns the job id.

    The reservation is taken in memory first and never depends on the write
    succeeding: losing the notepad must not also lose the protection that
    keeps the next turn out of this conversation.
    """
    job_id = uuid.uuid4().hex[:8]
    job = {
        "job_id": job_id,
        "session_id": session_id,
        "user_id": user_id,
        "owner": _owner,
        "pid": os.getpid(),
        "question": question[:500],
        "state": RUNNING,
        "started_at": time.time(),
        "finished_at": None,
        "answer": "",
        "durable": True,
    }
    _JOBS[job_id] = job
    _prune_memory()

    jobs = _prune(_load())
    jobs[job_id] = dict(job)
    try:
        _save(jobs)
    except JobStoreError as e:
        job["durable"] = False
        logger.error(
            "[JOBS] %s is running but was not written down (%s) — it will be "
            "invisible after a restart", job_id, e,
        )
    logger.info("[JOBS] %s started for session '%s'", job_id, session_id)
    return job_id


def finish(job_id: str, answer: str = "", failed: bool = False) -> None:
    """Close a job out and release its session.

    The in-process record is updated, not removed. Dropping it and then
    failing to write the file left the old `running` row on disk as the only
    answer, so a finished job went on reserving its conversation.
    """
    state = FAILED if failed else DONE
    finished_at = time.time()
    answer = (answer or "")[:2000]

    live = _JOBS.get(job_id)
    if live is not None:
        live.update(state=state, finished_at=finished_at, answer=answer)

    jobs = _load()
    job = jobs.get(job_id)
    if job is None:
        if live is None:
            return
        job = dict(live)
        jobs[job_id] = job
    job["state"] = state
    job["finished_at"] = finished_at
    job["answer"] = answer
    try:
        _save(_prune(jobs))
    except JobStoreError as e:
        if live is not None:
            live["durable"] = False
        logger.error("[JOBS] %s finished but could not be recorded: %s", job_id, e)
    _prune_memory()
    logger.info("[JOBS] %s %s", job_id, state)


def _merged() -> Dict[str, dict]:
    """Everything known about this owner's jobs, memory winning.

    Memory is the newer truth in both directions: a job whose start could not
    be written is only here, and a job whose end could not be written is
    `running` on disk and finished here.
    """
    merged = {job_id: dict(job) for job_id, job in _load().items()}
    merged.update({job_id: dict(job) for job_id, job in _JOBS.items()})
    return merged


def active_for_session(session_id: str) -> Optional[dict]:
    """The job still holding this session's conversation, if any."""
    for job in _merged().values():
        if job.get("session_id") == session_id and job.get("state") == RUNNING:
            return job
    return None


def latest_for_session(session_id: str) -> Optional[dict]:
    """The most recent job for this session, running or not."""
    candidates = [
        job for job in _merged().values() if job.get("session_id") == session_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda job: job.get("started_at", 0))


def get(job_id: str) -> Optional[dict]:
    return _merged().get(job_id)


def mark_orphans_interrupted() -> int:
    """Anything still `running` in THIS process's notepad is from a dead run.

    Called once when an interface comes up. The work cannot be resumed — the
    request was natural language and the runner it belonged to no longer
    exists — but leaving it `running` would reserve its session forever and
    silently block every later orchestrator turn in that conversation.

    Only this owner's file is touched. When every process shared one, any of
    them starting up could declare a colleague's live job interrupted and
    release a session that was still being written to.
    """
    jobs = _load()
    orphans = [
        job for job in jobs.values()
        if job.get("state") == RUNNING and job.get("job_id") not in _JOBS
    ]
    if not orphans:
        return 0
    for job in orphans:
        job["state"] = INTERRUPTED
        job["finished_at"] = time.time()
    try:
        _save(_prune(jobs))
    except JobStoreError as e:
        logger.error("[JOBS] Could not record interrupted jobs: %s", e)
    logger.warning(
        "[JOBS] %d job(s) were still running when the process died", len(orphans)
    )
    return len(orphans)


def _prune_memory() -> None:
    """Keep the in-process table from growing for the life of the process."""
    finished = [
        (job_id, job) for job_id, job in _JOBS.items()
        if job.get("state") != RUNNING
    ]
    excess = len(finished) - _MEMORY_MAX
    if excess <= 0:
        return
    finished.sort(key=lambda kv: kv[1].get("finished_at") or 0)
    for job_id, _ in finished[:excess]:
        del _JOBS[job_id]


def reset() -> None:
    """Drop the in-process table. Tests only."""
    _JOBS.clear()


def describe(job: Dict[str, Any]) -> str:
    """One spoken Croatian sentence about where a job got to."""
    state = job.get("state")
    if state == RUNNING:
        minutes = max(1, int((time.time() - job.get("started_at", 0)) // 60))
        return (
            f"Još radim na tome — traje {minutes} min. "
            f"Javim ti čim završim."
        )
    if state == DONE:
        answer = job.get("answer") or ""
        return answer or "Gotovo je."
    if state == FAILED:
        return "Nisam uspio dovršiti taj zadatak."
    return (
        "Taj se zadatak prekinuo jer se sustav u međuvremenu restartao. "
        "Reci mi ponovno pa krećem iznova."
    )
