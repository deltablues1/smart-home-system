"""A daily ceiling on model spend, enforced in code.

On 2026-08-30 false wake-word triggers drained the Anthropic credit overnight.
Nothing in the system noticed: token accounting recorded every call faithfully
and then did nothing with the number. Console limits help, but they arrive as a
hard cutoff with no warning and no record of what caused it.

This is the local half. Spend is accumulated per calendar day and persisted, so
a restart does not hand the day a fresh allowance — the failure mode that
matters is a loop that also restarts the process.

Deliberately not a rate limiter: bursts are fine, a day's worth of them is not.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: Optional[dict] = None


def store_path() -> Path:
    return Path(
        os.getenv("LLM_BUDGET_FILE", "")
        or Path(__file__).resolve().parents[1] / "data" / "llm_budget.json"
    )


def daily_budget_usd() -> float:
    """0 or unset disables the ceiling; spend is still recorded."""
    try:
        return max(0.0, float(os.getenv("DAILY_LLM_BUDGET_USD", "0")))
    except ValueError:
        return 0.0


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict:
    global _state
    if _state is not None and _state.get("day") == _today():
        return _state
    with _lock:
        loaded = {"day": _today(), "spent": 0.0}
        try:
            path = store_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("day") == _today():
                    loaded = {"day": data["day"], "spent": float(data.get("spent", 0.0))}
        except Exception as exc:
            # A broken ledger must not stop the assistant; it starts the day at
            # zero, which is the permissive direction and is stated in the log.
            logger.warning("Budget ledger unreadable (%s), starting the day at zero", exc)
        _state = loaded
        return _state


def _persist() -> None:
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(_state), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Could not persist the budget ledger to %s: %s", path, exc)


def record(cost_usd: float) -> float:
    """Add one call's cost to today's total. Returns the new total."""
    if not cost_usd or cost_usd < 0:
        return spent_today()
    state = _load()
    with _lock:
        state["spent"] = round(state["spent"] + float(cost_usd), 6)
        _persist()
    return state["spent"]


def spent_today() -> float:
    return _load()["spent"]


def remaining() -> Optional[float]:
    budget = daily_budget_usd()
    if budget <= 0:
        return None
    return max(0.0, budget - spent_today())


def over_budget() -> bool:
    budget = daily_budget_usd()
    return budget > 0 and spent_today() >= budget


def reset() -> None:
    """Tests only."""
    global _state
    _state = None
