"""One approval gate for every action with consequences.

The prompt is not a gate. "Wait for the user to confirm" is an instruction the
model can talk itself out of, and a model that believes the user already said
yes will act in the same turn it asked. Two hand-rolled gates grew out of that
realisation independently — one for MQTT switches, one for proposed meeting
slots — with different TTLs, different session semantics and one of them no
session scoping at all, so a turn in one conversation armed a pending action
from another. This is that mechanism, once.

The property that matters: an approval becomes redeemable only when a *new user
message* arrives. Proposing and executing inside one model turn is therefore
impossible, whatever the model believes. Redeeming consumes the approval, so a
repeated "da" — an echo, a re-transcription — cannot execute twice.

Two arming modes, because the two callers ask different questions:

- ``affirmative`` — "Ugasiti bojler?" needs an actual yes, and only the most
  recently asked question is armed. One "da" must not authorise a queue.
- ``next_turn``   — "Which of these three slots?" is answered by choosing, not
  by saying yes, so any next turn arms every slot proposed together.

State is per-process and in memory: one Pi, one worker. A restart drops pending
approvals, which fails safe (the action does not happen). Anything needing to
survive that belongs in services/hitl_firestore_service.py.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

AFFIRMATIVE = "affirmative"
NEXT_TURN = "next_turn"

DEFAULT_TTL_SECONDS = 120.0
# Choosing between proposed meeting slots is a slower conversation than
# confirming a switch, and the old calendar gate used 600s.
PROPOSAL_TTL_SECONDS = 600.0

_session_var: ContextVar[str] = ContextVar("approval_session", default="global")
# A run with nobody to ask. The scheduler fires jobs at 07:30 with no user in
# the loop, so a gated action there can never be confirmed — and must not be
# left pending either, or a "da" typed later in an unrelated channel could arm
# it. Autonomous runs are refused outright instead.
_autonomous_var: ContextVar[bool] = ContextVar("approval_autonomous", default=False)
_seq = itertools.count()


@dataclass
class _Pending:
    action_id: str
    session: str
    arm_mode: str
    created_at: float
    ttl: float
    seq: int
    question: str = ""
    lane: str = ""
    armed: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def expired(self, now: float) -> bool:
        return now - self.created_at > self.ttl


_PENDING: Dict[Tuple[str, str], _Pending] = {}

# "The user said yes this turn, but nothing was waiting for it."
# A gate that only ever arms on the NEXT message spends a turn whenever the
# model asks its own clarifying question first — measured 2026-09-04, deleting
# a calendar event took three turns because turn one was spent identifying
# which event. The consent is real, it just arrived before the attempt.
# session -> action ids a bare "da" may still authorise this turn. It used to
# be a plain flag, so a yes with nothing pending armed whatever action was
# registered next — including one the user had never been asked about. Now it
# carries the ids the gate actually held.
_affirmative_turn: Dict[str, set] = {}

# What was executed a moment ago, so a repeated "da" cannot run it twice.
_recently_redeemed: Dict[Tuple[str, str], float] = {}
REDEEM_MEMORY_SECONDS = 120.0


def allow_same_turn() -> bool:
    """Whether a yes may authorise an action attempted later in the same turn."""
    return os.getenv("APPROVAL_ALLOW_SAME_TURN", "true").lower() in (
        "1", "true", "yes", "on"
    )


def default_ttl() -> float:
    try:
        return float(os.getenv("APPROVAL_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    except ValueError:
        return DEFAULT_TTL_SECONDS


# --- session binding --------------------------------------------------------
# Tools run deep inside the agent call and cannot see the session id, so the
# interface binds it to the context before dispatching the turn.

def set_session(session_id: Optional[str]) -> None:
    _session_var.set(session_id or "global")


def current_session() -> str:
    return _session_var.get()


def set_user(user_id: Optional[str]) -> None:
    """Bind the turn's user, for flows that must name who is being asked."""
    _user_var.set(user_id or "")


def current_user() -> str:
    return _user_var.get()


_user_var: ContextVar[str] = ContextVar("approval_user", default="")


def set_autonomous(flag: bool) -> None:
    """Mark this run as having no user to ask (scheduler, background jobs)."""
    _autonomous_var.set(bool(flag))


def is_autonomous() -> bool:
    return _autonomous_var.get()


def _norm(session_id: Optional[str]) -> str:
    return session_id or "global"


# --- identity ---------------------------------------------------------------

def fingerprint(tool: str, **parts: Any) -> str:
    """A stable id for "this exact action with these exact arguments".

    Only the arguments that change what happens belong here. Including a
    free-text note would let a reworded retry slip past a consumed approval;
    leaving out the amount would let a confirmed 5 become an executed 50.
    """
    payload = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool}:{digest}"


# --- lifecycle --------------------------------------------------------------

def register(
    action_id: str,
    *,
    question: str = "",
    arm_mode: str = AFFIRMATIVE,
    lane: str = "",
    ttl: Optional[float] = None,
    session: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record that this action is waiting for the user. Never armed on creation."""
    sess = _norm(session or current_session())

    # The user's yes came before the attempt. Honour it once, for one action,
    # and never for something already executed on the back of the same word.
    armed_now = False
    if (
        arm_mode == AFFIRMATIVE
        and allow_same_turn()
        and action_id in _affirmative_turn.get(sess, ())
        and not _redeemed_recently(sess, action_id)
    ):
        armed_now = True
        # Spent. Not "one fewer": the whole bank goes, so a second attempt in
        # the same turn has to be asked about again.
        _affirmative_turn.pop(sess, None)
        logger.info("Approval armed by this turn's confirmation: %s", action_id)

    _PENDING[(sess, action_id)] = _Pending(
        action_id=action_id,
        session=sess,
        arm_mode=arm_mode,
        created_at=time.monotonic(),
        ttl=default_ttl() if ttl is None else ttl,
        seq=next(_seq),
        question=question,
        lane=lane,
        armed=armed_now,
        meta=dict(meta or {}),
    )


def _redeemed_recently(session: str, action_id: str) -> bool:
    stamp = _recently_redeemed.get((session, action_id))
    return stamp is not None and (time.monotonic() - stamp) < REDEEM_MEMORY_SECONDS


def _purge(now: float) -> None:
    for key, entry in list(_PENDING.items()):
        if entry.expired(now):
            del _PENDING[key]


def _live(session_id: str, now: float):
    return [e for (s, _), e in _PENDING.items() if s == session_id and not e.expired(now)]


def on_user_turn(
    session_id: Optional[str],
    *,
    affirmative: bool,
    held_actions: Iterable[str] = (),
) -> Optional[str]:
    """A new user message arrived. Returns the action_id armed by a yes, if any.

    Slot-style proposals arm on any turn — the user answers them by choosing.
    Yes/no approvals arm only on an actual yes, and only the newest: two
    questions pending and one "da" must authorise one of them, not both.
    A non-yes drops them, so "ne" and a change of subject both cancel.

    `held_actions` are the actions the gate stopped to ask about last turn.
    A "da" with nothing pending is banked only for those. Before this, it was
    banked for whatever action happened to be registered next, so a yes meant
    for one thing could authorise something the user was never shown.
    """
    sess = _norm(session_id)
    now = time.monotonic()
    _purge(now)

    armed_id = None
    had_affirmative_pending = any(
        e.arm_mode == AFFIRMATIVE for e in _live(sess, now)
    )
    # Banked only when the yes has nothing pending to arm directly, and only
    # for the LAST thing the gate asked about. Keeping the whole set meant one
    # "da" could redeem two of them, because register() removed only the id it
    # had just used. One answer, one action — and the newest is the one the
    # user was looking at, the same rule the pending branch below uses.
    asked = [a for a in held_actions if a]
    if affirmative and not had_affirmative_pending and asked:
        _affirmative_turn[sess] = {asked[-1]}
    else:
        _affirmative_turn.pop(sess, None)

    for entry in _live(sess, now):
        if entry.arm_mode == NEXT_TURN:
            entry.armed = True

    if affirmative:
        candidates = [e for e in _live(sess, now) if e.arm_mode == AFFIRMATIVE]
        if candidates:
            newest = max(candidates, key=lambda e: e.seq)
            newest.armed = True
            armed_id = newest.action_id
            for other in candidates:
                if other is not newest:
                    del _PENDING[(sess, other.action_id)]
    else:
        for entry in list(_live(sess, now)):
            if entry.arm_mode == AFFIRMATIVE:
                del _PENDING[(sess, entry.action_id)]

    return armed_id


def redeem(action_id: str, *, session: Optional[str] = None) -> bool:
    """Consume an armed, unexpired approval. False means: do not act."""
    sess = _norm(session or current_session())
    entry = _PENDING.get((sess, action_id))
    if entry is None:
        return False
    if entry.expired(time.monotonic()):
        del _PENDING[(sess, action_id)]
        return False
    if not entry.armed:
        return False
    del _PENDING[(sess, action_id)]
    _recently_redeemed[(sess, action_id)] = time.monotonic()
    if len(_recently_redeemed) > 2048:  # bounded memory
        _recently_redeemed.clear()
    return True


def has_pending(session_id: Optional[str]) -> bool:
    """Used by voice routing to keep a bare "da" in the lane that asked."""
    return bool(_live(_norm(session_id), time.monotonic()))


def _newest(session_id: Optional[str]):
    live = _live(_norm(session_id), time.monotonic())
    return max(live, key=lambda e: e.seq) if live else None


def pending_question(session_id: Optional[str]) -> Optional[str]:
    entry = _newest(session_id)
    return (entry.question or None) if entry else None


def pending_lane(session_id: Optional[str]) -> Optional[str]:
    """Which agent asked the question still waiting.

    A short "da" belongs back in the lane that asked for it. Assuming
    smart_home — as the MQTT-only version could — would send a confirmed
    stock movement to the light switches once other tools were gated.
    """
    entry = _newest(session_id)
    return (entry.lane or None) if entry else None


def cancel(session_id: Optional[str]) -> None:
    sess = _norm(session_id)
    for key in [k for k in _PENDING if k[0] == sess]:
        del _PENDING[key]


def reset() -> None:
    """Tests only. Clears pending state *and* the bound session.

    Leaving the session bound leaks across tests: a case that binds "session-a"
    makes the next one register under it, and an assertion about "global"
    then fails only when the suite runs in order.
    """
    _PENDING.clear()
    _affirmative_turn.clear()
    _recently_redeemed.clear()
    _session_var.set("global")
    _autonomous_var.set(False)
