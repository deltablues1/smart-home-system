"""Turn-gated confirmation for tools whose effects leave the house.

Until now exactly three MQTT switches and proposed meeting slots were defended
by code. Sending mail, sharing a document with the world and deleting a
calendar entry were defended by sentences in a prompt — which is the
same defence that fails whenever the model believes the user already agreed, and
the same defence that a prompt injection in a fetched web page or an email body
argues with directly.

This plugs into ADK's before_tool_callback, whose contract the loop guard in
adk_agent_factory already relies on: return a dict and the tool never runs, the
dict goes back to the model as the tool's result. So a gated call comes back as
"ask the user", in the model's own result channel, and the next real user
message is what makes it executable.

Gating is deliberately narrow. A tool that is annoying to confirm every time
gets confirmed for the cases that actually cost something: mail to an address
the house has never written to, sharing set to "anyone", and deleting a
calendar entry.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from services import approvals, known_recipients
from services.drive_links import extract_file_ids

logger = logging.getLogger(__name__)


def _recipients(args: Dict[str, Any]) -> list:
    out = []
    for field in ("to", "cc", "bcc"):
        value = args.get(field) or ""
        out.extend(part.strip() for part in str(value).replace(";", ",").split(",") if part.strip())
    return out


def _unknown_recipients(args: Dict[str, Any]) -> list:
    """Addresses the house has never written to and does not have on file.

    Filtering by domain was the first attempt and it asked about every client,
    which is most of the real mail — friction on the normal case and no extra
    safety, since a plausible domain is the easy half of a redirect to forge.
    A recipient nobody has ever written to is the actual signal."""
    return [a for a in _recipients(args) if not known_recipients.is_known(a)]


def _digest(text: Any) -> str:
    """A short, stable stand-in for a value too long to put in a key."""
    if not text:
        return ""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def _file_digest(path: Any) -> str:
    """A digest of the attachment's CONTENT, not its name.

    The path alone says nothing about what is being sent: a file can be
    rewritten between the moment the user is shown the mail and the moment it
    goes out, and the approval would still fit. An unreadable file gets a
    marker of its own rather than passing as "no attachment".
    """
    if not path:
        return ""
    try:
        data = Path(str(path)).read_bytes()
    except Exception:
        return f"unreadable:{_digest(path)}"
    return hashlib.sha256(data).hexdigest()[:16]


def _shared_documents(args: Dict[str, Any]) -> list:
    """Drive files this mail will hand the recipients access to.

    Sending a link is not the same intent as changing who may open the
    document, and the mailer does the second on its way to doing the first.
    They belong in the key, or an approval for "mail X about Y" also covers
    a re-issued call that quietly shares a different document.
    """
    return sorted(extract_file_ids(args.get("body", "")))


class _Rule:
    def __init__(
        self,
        when: Callable[[Dict[str, Any]], bool],
        key: Callable[[Dict[str, Any]], Dict[str, Any]],
        question: Callable[[Dict[str, Any]], str],
        lane: str = "",
        on_confirmed: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.when = when
        self.key = key
        self.question = question
        self.lane = lane
        self.on_confirmed = on_confirmed


def _shares_note(args: Dict[str, Any]) -> str:
    """Say out loud which documents the recipients are about to gain access to."""
    documents = _shared_documents(args)
    if not documents:
        return ""
    who = ", ".join(_recipients(args)) or "primateljima"
    return (
        f"; time daješ {who} pristup za čitanje dokumentima: "
        f"{', '.join(documents)}"
    )


RULES: Dict[str, _Rule] = {
    "gmail_send_message": _Rule(
        when=lambda a: bool(_unknown_recipients(a)),
        # Recipients and subject were the whole key, so the same approval
        # fitted a re-issued call with a different body, a different
        # attachment, and different documents shared along the way.
        key=lambda a: {
            "to": sorted(_recipients(a)),
            "subject": a.get("subject", ""),
            "body": _digest(a.get("body", "")),
            "attachment": _file_digest(a.get("attachment_path", "")),
            "shares": _shared_documents(a),
        },
        question=lambda a: (
            f"poslati mail na {', '.join(_unknown_recipients(a))} "
            f"({a.get('subject', 'bez naslova')})"
            + _shares_note(a)
        ),
        lane="mailer",
        # Confirmed once is known from then on, so the gate narrows to genuinely
        # new addresses instead of nagging about the same client every time.
        on_confirmed=lambda a: known_recipients.remember(_recipients(a)),
    ),
    "drive_share_file": _Rule(
        # Sharing with a named person is ordinary work; "anyone" is publishing.
        when=lambda a: str(a.get("type", "user")).lower() == "anyone",
        key=lambda a: {"file_id": a.get("file_id"), "type": a.get("type"), "role": a.get("role")},
        question=lambda a: f"dokument {a.get('file_id')} javno dostupan svima s linkom",
        lane="librarian",
    ),
    "calendar_delete_event": _Rule(
        when=lambda a: True,
        key=lambda a: {"event_id": a.get("event_id"), "calendar_id": a.get("calendar_id", "primary")},
        question=lambda a: f"obrisati termin {a.get('event_id')}",
        lane="secretary",
    ),
}


# How many times one action has already been held inside the current run.
# Asking the user is a thing you do once: on 2026-09-04 a model re-issued the
# same held call five times in a single turn, each a billed round-trip that
# could not possibly succeed, because nothing said "you already asked".
_holds_this_run: Dict[tuple, int] = {}


def _note_confirmation_needed(action: str) -> None:
    """Tell whoever is tracking this run that it stopped to ask."""
    try:
        from services import run_effects
        run_effects.note_needs_confirmation(action)
    except Exception:  # never let bookkeeping break the gate
        logger.debug("Could not record a needed confirmation", exc_info=True)


def _record_hold_for_run(action_id: str) -> None:
    """Every hold counts, not only the autonomous refusal.

    plan-execute reads this to tell "the step is waiting for a person" from
    "the step is done" — a distinction the answer text cannot carry, because
    the answer text is written by a model.
    """
    _note_confirmation_needed(action_id)


def _record_hold(tool_context, action_id: str) -> int:
    # Keyed on the user's session, not the ADK invocation id: the orchestrator
    # calls a worker as a sub-agent, and each of those calls is its own
    # invocation. Keying on invocation counted every retry as "attempt 1",
    # which is exactly the loop this counter exists to interrupt.
    key = (approvals.current_session(), action_id)
    count = _holds_this_run.get(key, 0) + 1
    _holds_this_run[key] = count
    if len(_holds_this_run) > 2048:  # bounded memory
        _holds_this_run.clear()
    return count


def _clear_holds(tool_context, action_id: str) -> None:
    _holds_this_run.pop((approvals.current_session(), action_id), None)


def take_holds(session_id: Optional[str] = None) -> list:
    """Which actions this session was asked about, clearing the count.

    Called once per user message. The ids go to `approvals.on_user_turn`,
    which is what lets a bare "da" arm the thing the user was actually shown
    and nothing else.

    Ordered, oldest first: a set loses which question was asked last, and the
    last one is the one the user is answering.
    """
    session = session_id or approvals.current_session()
    held = []
    for key in [k for k in _holds_this_run if k[0] == session]:
        held.append(key[1])
        del _holds_this_run[key]
    return held


def reset_holds(session_id: Optional[str] = None) -> None:
    """A new user message starts the count over: asking once per turn is fine."""
    take_holds(session_id)


def gate_enabled() -> bool:
    return os.getenv("APPROVAL_GATE_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def approval_before_tool(tool=None, args=None, tool_context=None, **_kwargs):
    """ADK before_tool_callback: hold a consequential call for a real user yes."""
    if not gate_enabled():
        return None

    name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
    rule = RULES.get(name)
    if rule is None:
        return None

    args = args or {}
    try:
        if not rule.when(args):
            return None
        question = rule.question(args)
    except Exception as exc:
        # Fail closed. A security control that opens on its own bug is not a
        # control: whatever made the rule throw is exactly the input nobody
        # anticipated, which is the last input to run unchecked.
        logger.error("Approval rule for '%s' failed — refusing the call: %s", name, exc)
        return {
            "error": (
                f"APPROVAL RULE FAILED za '{name}'. Radnja NIJE izvršena i neće "
                "biti dok se to ne popravi. Javi korisniku da je sigurnosna "
                "provjera pukla i da radnju treba napraviti ručno."
            )
        }

    # The fingerprint is computed before the autonomous branch so both paths
    # can record WHICH action was held, not just which tool. Two calls of the
    # same tool with different arguments are different actions.
    try:
        action_id = approvals.fingerprint(name, **rule.key(args))
    except Exception as exc:
        # Same reasoning as above: without a fingerprint there is nothing to
        # confirm against, so there is no way to let this through safely.
        logger.error("Approval key for '%s' failed — refusing the call: %s", name, exc)
        return {
            "error": (
                f"APPROVAL KEY FAILED za '{name}'. Radnja NIJE izvršena. Javi "
                "korisniku da je sigurnosna provjera pukla."
            )
        }

    if approvals.is_autonomous():
        # Nobody is listening. Registering here would leave a pending approval
        # that a later "da" in some other channel could arm, and returning
        # "ask the user" would send the model round the loop guard for nothing.
        logger.warning("[APPROVAL] refusing %s in an autonomous run: %s", name, question)
        _note_confirmation_needed(action_id)
        return {
            "status": "not_permitted",
            "action": name,
            "error": "AUTONOMOUS_RUN_NEEDS_CONFIRMATION",
            "message": (
                f"Radnja '{question}' traži potvrdu korisnika, a ovo je automatski "
                "posao bez korisnika. Nemoj ponavljati poziv — javi u odgovoru da "
                "radnja nije izvršena i zašto."
            ),
        }

    if approvals.redeem(action_id):
        logger.info("[APPROVAL] redeemed for %s", name)
        _clear_holds(tool_context, action_id)
        if rule.on_confirmed is not None:
            try:
                rule.on_confirmed(args)
            except Exception as exc:
                logger.warning("Post-approval hook for '%s' failed: %s", name, exc)
        return None

    approvals.register(action_id, question=question, lane=rule.lane)

    # register() arms immediately when the user already said yes this turn, so
    # ask again right here. Checking only before registering meant the hold was
    # decided before the consent was applied, and a confirmation that arrived
    # ahead of the attempt was recorded and then thrown away unused.
    if approvals.redeem(action_id):
        logger.info("[APPROVAL] %s armed by this turn's confirmation", name)
        _clear_holds(tool_context, action_id)
        if rule.on_confirmed is not None:
            try:
                rule.on_confirmed(args)
            except Exception as exc:
                logger.warning("Post-approval hook for '%s' failed: %s", name, exc)
        return None

    holds = _record_hold(tool_context, action_id)
    _record_hold_for_run(action_id)
    logger.info(
        "[APPROVAL] holding %s until the user confirms (attempt %d): %s",
        name, holds, question,
    )

    if holds >= 3:
        # Advice did not stop it: held four times in one turn on a calendar
        # deletion, 2026-09-04. An "error" key is the shape the loop guard
        # counts and the model treats as terminal, so this ends the turn
        # instead of spending more round-trips that cannot succeed.
        logger.warning(
            "[APPROVAL] %s held %d times in one turn — returning a hard stop",
            name, holds,
        )
        return {
            "error": (
                f"STOP: '{question}' je zadržano {holds} puta u ovom turnusu i "
                "NEĆE proći koliko god puta pokušao. Odobrenje se aktivira tek "
                "na korisnikovu sljedeću poruku. Ne zovi više nijedan alat. "
                "Postavi korisniku pitanje i završi odgovor. Ovo nije kvar nego "
                "sigurnosni korak — ne opisuj ga kao grešku."
            )
        }

    if holds > 1:
        # Repeating the call cannot help: the approval only arms on the user's
        # NEXT message, which cannot arrive while this turn is still running.
        return {
            "status": "needs_confirmation",
            "action": name,
            "question": question,
            "message": (
                f"Već si u ovom turnusu {holds} puta pokušao '{question}'. "
                "Ponavljanje NE MOŽE uspjeti — odobrenje se aktivira tek kad "
                "korisnik odgovori, a on ne može odgovoriti dok ti radiš. "
                "PRESTANI zvati ovaj alat, postavi korisniku pitanje i završi "
                "odgovor. OVO NIJE KVAR nego namjeran sigurnosni korak: pitaj "
                "mirno i normalno, ne opisuj ga korisniku kao grešku, petlju "
                "ili tehnički problem."
            ),
        }

    return {
        "status": "needs_confirmation",
        "action": name,
        "question": question,
        "message": (
            f"Radnja '{question}' čeka potvrdu korisnika i NIJE izvršena. "
            "Prenesi to pitanje korisniku i stani. OVO NIJE KVAR nego namjeran "
            "sigurnosni korak — pitaj mirno, jednom rečenicom, i ne opisuj ga "
            "kao grešku ili tehnički problem. Kad korisnik potvrdi, mora se "
            "ponoviti CIJELI zahtjev s identičnim argumentima — sama riječ "
            "'da' proslijeđena agentu ne znači ništa, jer agent nastaje iznova "
            "i ne pamti što je pitao. Ne mijenjaj argumente i ne pretpostavljaj "
            "potvrdu."
        ),
    }
