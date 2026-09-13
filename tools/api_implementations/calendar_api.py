"""
Google Calendar API Implementation

Real Calendar API functions using Google Calendar API v3
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from datetime import datetime
import logging
import os


def _default_timezone() -> str:
    """User's IANA timezone for event bodies (was hardcoded 'UTC')."""
    return os.getenv("DEFAULT_USER_TIMEZONE", "Europe/Zagreb")


def _parse_rfc3339(value: str) -> Optional[datetime]:
    """Parse an RFC3339 timestamp; returns None when unparseable."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

from tools.resilience.retry_handler import with_retry, RetryConfig, with_quota_retry
from tools.resilience.circuit_breaker import with_circuit_breaker
from tools.resilience.rate_limiter import with_rate_limit
from tools.resilience.cache import with_cache, invalidates_cache
from tools.google_api_client import aexecute

logger = logging.getLogger(__name__)


# ============================================================================
# CALENDAR API FUNCTIONS
# ============================================================================

@with_circuit_breaker("calendar")
@with_cache("calendar", ttl=180, user_id_param="credentials")  # Cache for 3 min (events change frequently)
@with_rate_limit("calendar", user_id_param="credentials")
@with_quota_retry()  # 15s, 30s, 60s delays for API quota limits
async def calendar_list_events(
    credentials: Credentials,
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 10
) -> Dict[str, Any]:
    """
    List calendar events within a time range

    Args:
        credentials: OAuth2 credentials
        calendar_id: Calendar ID (default: 'primary')
        time_min: Start time in RFC3339 format (e.g., '2024-01-01T00:00:00Z')
        time_max: End time in RFC3339 format
        max_results: Maximum number of events (default: 10)

    Returns:
        Dictionary with list of events

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"Listing Calendar events: calendar={calendar_id}, max={max_results}")

        # Build request parameters
        request_params = {
            'calendarId': calendar_id,
            'maxResults': max_results,
            'singleEvents': True,
            'orderBy': 'startTime'
        }

        if time_min:
            request_params['timeMin'] = time_min
        if time_max:
            request_params['timeMax'] = time_max

        # List events
        events_result = await aexecute(service.events().list(**request_params))
        events = events_result.get('items', [])

        # Format events
        formatted_events = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))

            formatted_events.append({
                'id': event['id'],
                'summary': event.get('summary', 'No title'),
                'description': event.get('description', ''),
                'start': start,
                'end': end,
                'location': event.get('location', ''),
                'attendees': [a.get('email') for a in event.get('attendees', [])],
                'html_link': event.get('htmlLink')
            })

        logger.info(f"Found {len(formatted_events)} events")

        return {
            'events': formatted_events,
            'count': len(formatted_events),
            'calendar_id': calendar_id
        }

    except HttpError as e:
        logger.error(f"Failed to list Calendar events: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_list_events: {e}")
        raise


@with_circuit_breaker("calendar")
@with_cache("calendar", ttl=180, user_id_param="credentials")  # Cache for 3 min
@with_rate_limit("calendar", user_id_param="credentials")
@with_quota_retry()  # 15s, 30s, 60s delays for API quota limits
async def calendar_get_event(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = "primary"
) -> Dict[str, Any]:
    """
    Get details of a specific calendar event

    Args:
        credentials: OAuth2 credentials
        event_id: Calendar event ID
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with event details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"Getting Calendar event: {event_id}")

        # Get event
        event = await aexecute(service.events().get(
            calendarId=calendar_id,
            eventId=event_id
        ))

        # Format event
        start = event['start'].get('dateTime', event['start'].get('date'))
        end = event['end'].get('dateTime', event['end'].get('date'))

        formatted_event = {
            'id': event['id'],
            'summary': event.get('summary', 'No title'),
            'description': event.get('description', ''),
            'start': start,
            'end': end,
            'location': event.get('location', ''),
            'attendees': [
                {
                    'email': a.get('email'),
                    'response_status': a.get('responseStatus')
                }
                for a in event.get('attendees', [])
            ],
            'html_link': event.get('htmlLink'),
            'created': event.get('created'),
            'updated': event.get('updated'),
            'creator': event.get('creator', {}).get('email')
        }

        logger.info(f"Event retrieved: {event.get('summary')}")

        return formatted_event

    except HttpError as e:
        logger.error(f"Failed to get Calendar event: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_get_event: {e}")
        raise


@with_circuit_breaker("calendar")
# NOTE: no retry here — create is not idempotent. The quota-retry wrapper also
# retries 5xx/timeouts, and a timeout after Google accepted the insert would
# create the event twice (same reasoning as gmail_send_message).
@invalidates_cache("calendar")
async def calendar_create_event(
    credentials: Credentials,
    summary: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary",
    timezone: Optional[str] = None,
    add_meet_link: bool = False,
    send_updates: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new calendar event

    Args:
        credentials: OAuth2 credentials
        summary: Event title/summary
        start_time: Start time in RFC3339 format
        end_time: End time in RFC3339 format
        description: Event description (optional)
        location: Event location (optional)
        attendees: List of attendee email addresses (optional)
        calendar_id: Calendar ID (default: 'primary')
        timezone: IANA timezone for the event (default: user's timezone,
            Europe/Zagreb unless DEFAULT_USER_TIMEZONE overrides it)
        add_meet_link: Attach a Google Meet conference to the event
        send_updates: Invitation email policy: "all", "externalOnly" or
            "none" (None = API default, i.e. no explicit choice)

    Returns:
        Dictionary with created event details (incl. meet_link when requested)

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        # Validate the time range before touching the API.
        start_dt = _parse_rfc3339(start_time)
        end_dt = _parse_rfc3339(end_time)
        if start_dt is None or end_dt is None:
            return {
                'status': 'error',
                'error': f"Invalid RFC3339 time: start={start_time!r}, end={end_time!r}",
            }
        # Compare only when both are naive or both aware (mixed can't be compared).
        if (start_dt.tzinfo is None) == (end_dt.tzinfo is None) and end_dt <= start_dt:
            return {
                'status': 'error',
                'error': f"Event end ({end_time}) must be after start ({start_time})",
            }

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"Creating Calendar event: {summary}")

        tz = timezone or _default_timezone()

        # Build event object
        event = {
            'summary': summary,
            'start': {
                'dateTime': start_time,
                'timeZone': tz
            },
            'end': {
                'dateTime': end_time,
                'timeZone': tz
            }
        }

        if description:
            event['description'] = description
        if location:
            event['location'] = location
        if attendees:
            event['attendees'] = [{'email': email} for email in attendees]

        insert_kwargs: Dict[str, Any] = {
            'calendarId': calendar_id,
            'body': event,
        }
        if add_meet_link:
            import uuid
            event['conferenceData'] = {
                'createRequest': {
                    'requestId': uuid.uuid4().hex,
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'},
                }
            }
            insert_kwargs['conferenceDataVersion'] = 1
        if send_updates:
            insert_kwargs['sendUpdates'] = send_updates

        # Create event
        created_event = await aexecute(service.events().insert(**insert_kwargs))

        logger.info(f"Event created successfully: {created_event['id']}")

        return {
            'id': created_event['id'],
            'summary': created_event.get('summary'),
            'start': created_event['start'].get('dateTime'),
            'end': created_event['end'].get('dateTime'),
            'html_link': created_event.get('htmlLink'),
            'meet_link': created_event.get('hangoutLink'),
            'attendees': [a.get('email') for a in created_event.get('attendees', [])],
            'status': 'created'
        }

    except HttpError as e:
        logger.error(f"Failed to create Calendar event: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_create_event: {e}")
        raise


@with_circuit_breaker("calendar")
@with_quota_retry()  # 15s, 30s, 60s delays for API quota limits
@invalidates_cache("calendar")
async def calendar_update_event(
    credentials: Credentials,
    event_id: str,
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary"
) -> Dict[str, Any]:
    """
    Update an existing calendar event

    Args:
        credentials: OAuth2 credentials
        event_id: Calendar event ID
        summary: New event title (optional)
        start_time: New start time in RFC3339 format (optional)
        end_time: New end time in RFC3339 format (optional)
        description: New description (optional)
        location: New location (optional)
        attendees: Replacement list of attendee emails (optional; omit to keep current)
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with updated event details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"Updating Calendar event: {event_id}")

        # Get current event
        event = await aexecute(service.events().get(
            calendarId=calendar_id,
            eventId=event_id
        ))

        # Update fields
        if summary:
            event['summary'] = summary
        if start_time:
            event['start']['dateTime'] = start_time
        if end_time:
            event['end']['dateTime'] = end_time
        if description:
            event['description'] = description
        if location:
            event['location'] = location
        if attendees is not None:
            event['attendees'] = [{'email': email} for email in attendees]

        # Reject an inverted time range after applying the changes.
        new_start = _parse_rfc3339(event.get('start', {}).get('dateTime', ''))
        new_end = _parse_rfc3339(event.get('end', {}).get('dateTime', ''))
        if (
            new_start is not None and new_end is not None
            and (new_start.tzinfo is None) == (new_end.tzinfo is None)
            and new_end <= new_start
        ):
            return {
                'status': 'error',
                'error': (
                    f"Event end ({event['end'].get('dateTime')}) must be after "
                    f"start ({event['start'].get('dateTime')})"
                ),
            }

        # Update event
        updated_event = await aexecute(service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event
        ))

        logger.info(f"Event updated successfully: {event_id}")

        return {
            'id': updated_event['id'],
            'summary': updated_event.get('summary'),
            'start': updated_event['start'].get('dateTime'),
            'end': updated_event['end'].get('dateTime'),
            'updated': updated_event.get('updated'),
            'status': 'updated'
        }

    except HttpError as e:
        logger.error(f"Failed to update Calendar event: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_update_event: {e}")
        raise


@with_circuit_breaker("calendar")
@with_quota_retry()  # 15s, 30s, 60s delays for API quota limits
@invalidates_cache("calendar")
async def calendar_delete_event(
    credentials: Credentials,
    event_id: str,
    calendar_id: str = "primary"
) -> Dict[str, Any]:
    """
    Delete a calendar event

    Args:
        credentials: OAuth2 credentials
        event_id: Calendar event ID
        calendar_id: Calendar ID (default: 'primary')

    Returns:
        Dictionary with deletion status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"Deleting Calendar event: {event_id}")

        # Delete event
        await aexecute(service.events().delete(
            calendarId=calendar_id,
            eventId=event_id
        ))

        logger.info(f"Event deleted successfully: {event_id}")

        return {
            'id': event_id,
            'calendar_id': calendar_id,
            'status': 'deleted'
        }

    except HttpError as e:
        logger.error(f"Failed to delete Calendar event: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_delete_event: {e}")
        raise


@with_circuit_breaker("calendar")
@with_rate_limit("calendar", user_id_param="credentials")
@with_quota_retry()  # 15s, 30s, 60s delays for API quota limits
async def calendar_freebusy(
    credentials: Credentials,
    time_min: str,
    time_max: str,
    emails: List[str],
    timezone: Optional[str] = None
) -> Dict[str, Any]:
    """
    Query busy intervals for a set of calendars (FreeBusy API).

    External calendars often deny free/busy access — those attendees are
    reported under "unknown" instead of failing the whole query.

    Args:
        credentials: OAuth2 credentials
        time_min: Window start in RFC3339 format
        time_max: Window end in RFC3339 format
        emails: Calendar IDs / attendee email addresses to query
        timezone: IANA timezone for the response (default: user's timezone)

    Returns:
        {
            'busy': {email: [{'start': ..., 'end': ...}, ...]},
            'unknown': [emails whose availability could not be read],
            'time_min': ..., 'time_max': ...
        }
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.calendar_service()

        logger.info(f"FreeBusy query for {len(emails)} calendars")

        body = {
            'timeMin': time_min,
            'timeMax': time_max,
            'timeZone': timezone or _default_timezone(),
            'items': [{'id': email} for email in emails],
        }
        response = await aexecute(service.freebusy().query(body=body))

        busy: Dict[str, List[Dict[str, str]]] = {}
        unknown: List[str] = []
        for email, calendar_info in (response.get('calendars') or {}).items():
            if calendar_info.get('errors'):
                unknown.append(email)
                continue
            busy[email] = [
                {'start': interval.get('start'), 'end': interval.get('end')}
                for interval in calendar_info.get('busy', [])
            ]

        return {
            'busy': busy,
            'unknown': unknown,
            'time_min': time_min,
            'time_max': time_max,
        }

    except HttpError as e:
        logger.error(f"FreeBusy query failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in calendar_freebusy: {e}")
        raise


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_calendar_tools(tool_registry):
    """
    Register all Calendar tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # calendar_list_events
    tool_registry.register_tool(
        name="calendar_list_events",
        function=calendar_list_events,
        description="List calendar events within a time range.",
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"},
                "time_min": {"type": "string", "description": "Start time in RFC3339 format"},
                "time_max": {"type": "string", "description": "End time in RFC3339 format"},
                "max_results": {"type": "integer", "description": "Maximum number of events", "default": 10}
            },
            "required": ["time_min"]
        }
    )

    # calendar_get_event
    tool_registry.register_tool(
        name="calendar_get_event",
        function=calendar_get_event,
        description="Get details of a specific calendar event.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Calendar event ID"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"}
            },
            "required": ["event_id"]
        }
    )

    # calendar_create_event
    tool_registry.register_tool(
        name="calendar_create_event",
        function=calendar_create_event,
        description="Create a new calendar event.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "Start time in RFC3339 format"},
                "end_time": {"type": "string", "description": "End time in RFC3339 format"},
                "description": {"type": "string", "description": "Event description (optional)"},
                "location": {"type": "string", "description": "Event location (optional)"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee emails (optional)"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"}
            },
            "required": ["summary", "start_time", "end_time"]
        }
    )

    # calendar_update_event
    tool_registry.register_tool(
        name="calendar_update_event",
        function=calendar_update_event,
        description="Update an existing calendar event.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID"},
                "summary": {"type": "string", "description": "New title (optional)"},
                "start_time": {"type": "string", "description": "New start time (optional)"},
                "end_time": {"type": "string", "description": "New end time (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "location": {"type": "string", "description": "New location (optional)"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"}
            },
            "required": ["event_id"]
        }
    )

    # calendar_delete_event
    tool_registry.register_tool(
        name="calendar_delete_event",
        function=calendar_delete_event,
        description="Delete a calendar event.",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID"},
                "calendar_id": {"type": "string", "description": "Calendar ID (default: 'primary')", "default": "primary"}
            },
            "required": ["event_id"]
        }
    )

    logger.info("Calendar tools registered successfully")
