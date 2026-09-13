"""
Calendar ADK Tools

ADK-compatible wrappers for Google Calendar operations:
- List events within time range
- Get event details
- Create new events
- Update existing events
- Delete events

Includes timezone-aware datetime handling.
"""

import logging
import os
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


def _get_credentials():
    """Get OAuth credentials from token file"""
    try:
        from auth.oauth_manager import get_oauth_manager
        oauth_manager = get_oauth_manager()
        creds = oauth_manager.get_credentials()
        if creds and creds.valid:
            return creds
        logger.warning("No valid credentials available for Calendar operations")
        return None
    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


async def calendar_list_events(
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    calendar_id: str = "primary",
    max_results: int = 10
) -> dict:
    """
    List calendar events within a time range.

    Retrieves events from the specified calendar between time_min and time_max.
    Useful for checking availability, finding conflicts, and viewing schedule.

    Args:
        time_min: Start time in RFC3339 format (optional, defaults to now)
                  Example: '2024-01-01T00:00:00Z'
        time_max: End time in RFC3339 format (optional, defaults to 7 days from time_min)
        calendar_id: Calendar ID (default: 'primary' for main calendar)
        max_results: Maximum number of events to return (default: 10)

    Returns:
        Dictionary with events array and calendar metadata
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    # If time_min not provided, use current time
    if time_min is None:
        from datetime import datetime, timezone
        time_min = datetime.now(timezone.utc).isoformat()

    try:
        from tools.api_implementations.calendar_api import calendar_list_events as calendar_list_impl

        result = await calendar_list_impl(creds, calendar_id, time_min, time_max, max_results)
        return result
    except Exception as e:
        logger.error(f"Failed to list calendar events: {e}")
        return {"error": str(e), "time_min": time_min}


async def calendar_get_event(
    event_id: str,
    calendar_id: str = "primary"
) -> dict:
    """
    Get details of a specific calendar event.

    Retrieves full event information including title, time, location,
    attendees, and description.

    Args:
        event_id: Calendar event ID
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with event details
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.calendar_api import calendar_get_event as calendar_get_impl

        # Correct parameter order: credentials, event_id, calendar_id
        result = await calendar_get_impl(creds, event_id, calendar_id)
        return result
    except Exception as e:
        logger.error(f"Failed to get calendar event {event_id}: {e}")
        return {"error": str(e), "event_id": event_id}


async def calendar_create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary"
) -> dict:
    """
    Create a new calendar event.

    Creates an event with specified details. Use RFC3339 format for times
    with explicit timezone (e.g., '2024-01-15T14:00:00-05:00').

    Args:
        summary: Event title/summary
        start_time: Start time in RFC3339 format (e.g., '2024-01-15T14:00:00-05:00')
        end_time: End time in RFC3339 format
        description: Event description (optional)
        location: Event location (optional)
        attendees: List of attendee email addresses (optional)
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with created event details including event_id and event_url
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    # Events WITH attendees are meetings — they must go through the gated
    # meeting flow (propose -> user choice -> calendar_create_meeting).
    # Without this, the generic create was a full bypass of that gate.
    if attendees:
        return {
            "status": "needs_confirmation",
            "message": (
                "Događaji sa sudionicima idu kroz meeting tok: "
                "calendar_propose_meeting_slots (ili potvrda termina s "
                "korisnikom) pa calendar_create_meeting. "
                "calendar_create_event služi samo za događaje bez sudionika."
            ),
        }

    try:
        from tools.api_implementations.calendar_api import calendar_create_event as calendar_create_impl

        # Correct parameter order: credentials, summary, start_time, end_time, description, location, attendees, calendar_id
        result = await calendar_create_impl(
            creds, summary, start_time, end_time,
            description, location, None, calendar_id
        )
        return result
    except Exception as e:
        logger.error(f"Failed to create calendar event: {e}")
        return {"error": str(e), "summary": summary}


async def calendar_update_event(
    event_id: str,
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary"
) -> dict:
    """
    Update an existing calendar event.

    Updates specified fields of an event. Only provide fields you want to change.
    Other fields will remain unchanged.

    Args:
        event_id: Calendar event ID
        summary: New event title (optional)
        start_time: New start time in RFC3339 format (optional)
        end_time: New end time in RFC3339 format (optional)
        description: New description (optional)
        location: New location (optional)
        attendees: New list of attendee emails (optional)
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with updated event details
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.calendar_api import calendar_update_event as calendar_update_impl

        result = await calendar_update_impl(
            creds, event_id, summary, start_time, end_time,
            description, location, attendees=attendees, calendar_id=calendar_id
        )
        return result
    except Exception as e:
        logger.error(f"Failed to update calendar event {event_id}: {e}")
        return {"error": str(e), "event_id": event_id}


async def calendar_delete_event(
    event_id: str,
    calendar_id: str = "primary",
    confirm: bool = False
) -> dict:
    """
    Delete a calendar event. PERMANENT — requires explicit confirmation.

    Call with confirm=False first: it returns the event summary and asks for
    confirmation. Only call with confirm=True after the user explicitly
    confirmed deleting THIS event.

    Args:
        event_id: Calendar event ID
        calendar_id: Calendar ID (default: 'primary')
        confirm: Must be True to actually delete (set only after user confirms)

    Returns:
        Dictionary with deletion confirmation, or a needs_confirmation request
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    if not confirm:
        summary = None
        try:
            from tools.api_implementations.calendar_api import (
                calendar_get_event as calendar_get_impl,
            )
            event = await calendar_get_impl(creds, event_id, calendar_id)
            if isinstance(event, dict):
                summary = event.get("summary")
        except Exception as e:
            logger.warning(f"Could not fetch event {event_id} for confirmation: {e}")
        return {
            "status": "needs_confirmation",
            "event_id": event_id,
            "summary": summary,
            "message": (
                "Brisanje događaja je trajno. Potvrdi s korisnikom koji događaj "
                "se briše, pa ponovi poziv s confirm=True."
            ),
        }

    try:
        from tools.api_implementations.calendar_api import calendar_delete_event as calendar_delete_impl

        # Correct parameter order: credentials, event_id, calendar_id
        result = await calendar_delete_impl(creds, event_id, calendar_id)
        return result
    except Exception as e:
        logger.error(f"Failed to delete calendar event {event_id}: {e}")
        return {"error": str(e), "event_id": event_id}


# --- Meeting proposal gate ---------------------------------------------------
# "Wait for the user's choice" must not live only in the prompt: without a gate
# the model can create a meeting immediately, or twice. A slot becomes
# redeemable only after the NEXT user turn arrives, so proposing and creating in
# the SAME model turn is impossible, and consume-on-use prevents double creates.
#
# The mechanism is services/approvals.py in NEXT_TURN mode: choosing among
# proposed slots IS the confirmation, so any next turn arms them — unlike a
# yes/no approval, which needs an actual yes. It is also session-scoped now;
# the local version armed every pending slot in the process, so a turn in one
# conversation could unlock a proposal made in another.
from services import approvals as _approvals

_PROPOSAL_LANE = "secretary"


def _norm_slot_time(value: str) -> str:
    from datetime import datetime, timezone as _tz

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo(os.getenv("DEFAULT_USER_TIMEZONE", "Europe/Zagreb")))
    return dt.astimezone(_tz.utc).isoformat(timespec="seconds")


def _norm_attendees(emails) -> list:
    """Sorted, lower-cased, de-duplicated. The set, not the order it arrived in."""
    seen = []
    for email in emails or []:
        address = str(email).strip().lower()
        if address and address not in seen:
            seen.append(address)
    return sorted(seen)


def _slot_action_id(start_iso: str, end_iso: str, attendees=None) -> str:
    """Identity of "this meeting", not just "this hour".

    Attendees belong in it: approving a slot for one set of people used to
    redeem a create for the same slot with an entirely different guest list.

    The summary does not, and cannot — at proposal time the user has not said
    what the meeting is called. Requiring it would mean no proposal ever
    matched its own creation, and every meeting would fall through to the
    custom-time path, which turns a real gate into noise.
    """
    return _approvals.fingerprint(
        "calendar_create_meeting",
        start=_norm_slot_time(start_iso),
        end=_norm_slot_time(end_iso),
        attendees=_norm_attendees(attendees),
    )


def _register_slot(start_iso: str, end_iso: str, attendees=None) -> None:
    _approvals.register(
        _slot_action_id(start_iso, end_iso, attendees),
        question=f"{start_iso} - {end_iso}",
        arm_mode=_approvals.NEXT_TURN,
        ttl=_approvals.PROPOSAL_TTL_SECONDS,
        lane=_PROPOSAL_LANE,
    )


def _register_proposed_slots(slots, attendees=None) -> None:
    for start, end in slots:
        _register_slot(start.isoformat(), end.isoformat(), attendees)


def arm_pending_proposals(session_id=None) -> None:
    """A new user turn arrived: arm that session's proposed slots."""
    _approvals.on_user_turn(
        session_id if session_id is not None else _approvals.current_session(),
        affirmative=False,
    )


def _consume_proposed_slot(start_time: str, end_time: str, attendees=None) -> bool:
    """Consume an ARMED slot (proposed/registered in an earlier turn)."""
    try:
        action_id = _slot_action_id(start_time, end_time, attendees)
    except ValueError:
        return False
    return _approvals.redeem(action_id)



async def calendar_check_freebusy(
    emails: List[str],
    time_min: str,
    time_max: str
) -> dict:
    """
    Check when people are busy (Google Calendar FreeBusy).

    Args:
        emails: Attendee email addresses (include the organizer's own
                calendar as "primary" or their email)
        time_min: Window start in RFC3339 format (e.g. '2026-07-13T00:00:00+02:00')
        time_max: Window end in RFC3339 format

    Returns:
        Dictionary with 'busy' intervals per email and 'unknown' for
        attendees whose availability could not be read (external calendars).
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.calendar_api import calendar_freebusy

        return await calendar_freebusy(creds, time_min, time_max, emails)
    except Exception as e:
        logger.error(f"FreeBusy check failed: {e}")
        return {"error": str(e), "status": "error"}


async def calendar_propose_meeting_slots(
    attendee_emails: List[str],
    duration_minutes: int = 60,
    window_days: int = 5,
    working_hours_start: int = 9,
    working_hours_end: int = 17
) -> dict:
    """
    Propose up to 3 meeting slots that are free for the organizer AND all
    attendees whose calendars are readable.

    Args:
        attendee_emails: Attendee email addresses (organizer's primary
                         calendar is included automatically)
        duration_minutes: Meeting length (default 60)
        window_days: How many days ahead to search (default 5)
        working_hours_start: Local hour meetings may start (default 9)
        working_hours_end: Local hour meetings must end by (default 17)

    Returns:
        Dictionary with 'slots' (RFC3339 start/end pairs), a Croatian
        'proposal' text to present to the user, and 'unknown_availability'
        listing attendees whose calendars could not be read — tell the user
        those attendees' availability is NOT verified.
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from services.meeting_scheduler import (
            compute_free_slots, format_slot_proposal_hr,
        )
        from tools.api_implementations.calendar_api import calendar_freebusy

        tz_name = os.getenv("DEFAULT_USER_TIMEZONE", "Europe/Zagreb")
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        window_start = now + timedelta(minutes=30)
        window_end = now + timedelta(days=window_days)

        calendars = ["primary"] + [e for e in attendee_emails if e]
        freebusy = await calendar_freebusy(
            creds,
            window_start.isoformat(),
            window_end.isoformat(),
            calendars,
        )

        slots = compute_free_slots(
            freebusy.get("busy", {}),
            window_start,
            window_end,
            duration_minutes,
            working_hours=(working_hours_start, working_hours_end),
            tz_name=tz_name,
        )
        # Only proposed slots may be turned into meetings (create-gate), and
        # only for the people they were proposed for.
        _register_proposed_slots(slots, attendee_emails)

        return {
            "status": "ok",
            "slots": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in slots
            ],
            "proposal": format_slot_proposal_hr(slots),
            "unknown_availability": freebusy.get("unknown", []),
        }
    except Exception as e:
        logger.error(f"Slot proposal failed: {e}")
        return {"error": str(e), "status": "error"}


async def calendar_create_meeting(
    summary: str,
    start_time: str,
    end_time: str,
    attendee_emails: List[str],
    description: Optional[str] = None,
    add_meet_link: bool = True,
    send_updates: str = "all",
    user_confirmed_custom_time: bool = False
) -> dict:
    """
    Create a meeting: calendar event with attendees and a Google Meet link.

    TURN-GATED: a slot (from calendar_propose_meeting_slots OR a first call
    with a custom time) becomes usable only after the USER's next message
    arrives — creating a meeting in the same turn as the proposal is
    impossible, and each slot is single-use (no double create). This is an
    inter-turn barrier, not NL proof of the user's exact choice: you MUST
    still present the time and honor the user's reply. With
    send_updates="all" Google Calendar emails the invitations itself — do
    NOT additionally auto-send an email; offer a follow-up DRAFT instead.

    Args:
        summary: Meeting title
        start_time: Start in RFC3339 format (confirmed slot)
        end_time: End in RFC3339 format
        attendee_emails: Attendee email addresses
        description: Optional agenda/description
        add_meet_link: Attach Google Meet (default True)
        send_updates: "all" (default, Google sends invites), "externalOnly" or "none"
        user_confirmed_custom_time: True ONLY when the user personally
            dictated this exact time (bypasses the proposed-slot check)

    Returns:
        Dictionary with event id, html_link, meet_link and attendees,
        or needs_confirmation when the slot was never proposed/confirmed.
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    if not _consume_proposed_slot(start_time, end_time, attendee_emails):
        # Not an armed proposed slot. Custom (user-dictated) times go through
        # the same turn gate: register now, demand the user's reply, accept
        # only on the NEXT turn with the explicit flag. The model cannot
        # bypass the gate by setting the flag itself in the same turn.
        try:
            _register_slot(start_time, end_time, attendee_emails)
        except ValueError:
            return {
                "status": "error",
                "error": f"Invalid RFC3339 time: {start_time!r} / {end_time!r}",
            }
        return {
            "status": "needs_confirmation",
            "message": (
                "Ovaj termin nije potvrđen. Predstavi ga korisniku i ČEKAJ "
                "njegov odgovor u sljedećoj poruci; nakon potvrde ponovi poziv "
                "(za termin koji je korisnik osobno diktirao proslijedi "
                "user_confirmed_custom_time=True). Za nove prijedloge koristi "
                "calendar_propose_meeting_slots."
            ),
        }

    try:
        from tools.api_implementations.calendar_api import (
            calendar_create_event as calendar_create_impl,
        )

        return await calendar_create_impl(
            creds, summary, start_time, end_time,
            description=description,
            attendees=attendee_emails,
            add_meet_link=add_meet_link,
            send_updates=send_updates,
        )
    except Exception as e:
        logger.error(f"Meeting creation failed: {e}")
        return {"error": str(e), "status": "error"}


def get_calendar_adk_tools(credentials=None) -> List:
    """
    Get all Calendar ADK tools as plain Python functions.

    ADK automatically wraps these functions as tools based on:
    - Function signature (type hints)
    - Docstring (description and parameter docs)

    Args:
        credentials: Not used - included for API compatibility. Tools use OAuth from token.

    Returns:
        List of calendar tool functions
    """
    tools = [
        calendar_list_events,
        calendar_get_event,
        calendar_create_event,
        calendar_update_event,
        calendar_delete_event,
        calendar_check_freebusy,
        calendar_propose_meeting_slots,
        calendar_create_meeting,
    ]

    logger.info(f"Calendar ADK tools loaded: {len(tools)} tools")
    return tools


if __name__ == "__main__":
    # Test tool loading
    tools = get_calendar_adk_tools()
    print(f"[OK] Calendar ADK tools loaded: {len(tools)} tools")

    for tool in tools:
        print(f"   - {tool.__name__}")

    print("\n[CAPABILITIES] Google Calendar Operations:")
    print("   - List events in time range")
    print("   - Get event details")
    print("   - Create new events")
    print("   - Update existing events")
    print("   - Delete events")
    print("   - Timezone-aware scheduling")
    print("   - Conflict detection support")
