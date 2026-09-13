"""Daily briefing: one deterministic read-only aggregate of the user's day.

Priority unread email, today's calendar, due/overdue tasks, weather and a
smart-home snapshot — collected by calling the existing API implementations
directly (the orchestrator fan-out is minutes-slow and this needs no LLM tool
calling). One optional LLM pass summarizes the collected data; the
deterministic Croatian template is the guaranteed fallback.

Every section is fetched in its own try/except: a dead section becomes
"nedostupno" and never fails the whole briefing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from config.user_context import UserContext, get_default_user_context

logger = logging.getLogger(__name__)

_HR_WEEKDAYS = (
    "ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja",
)


def _get_credentials():
    try:
        from auth.oauth_manager import get_oauth_manager

        creds = get_oauth_manager().get_credentials()
        if creds and creds.valid:
            return creds
    except Exception as e:
        logger.warning(f"Briefing: credentials unavailable: {e}")
    return None


def _day_bounds(tz_name: str) -> tuple[datetime, datetime]:
    now = datetime.now(ZoneInfo(tz_name))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------------------
# Collectors — each returns a plain dict/list or raises (caught by collect)
# ---------------------------------------------------------------------------

async def _collect_emails(creds) -> list[dict]:
    # Recency-only ("is:unread newer_than:1d"), NOT priority-ranked — a real
    # priority inbox would need sender/thread importance signals.
    from tools.api_implementations.gmail_api import gmail_search_threads

    result = await gmail_search_threads(creds, "is:unread newer_than:1d", 10)
    return [
        {
            "from": t.get("from", ""),
            "subject": t.get("subject", "(bez naslova)"),
        }
        for t in result.get("threads", [])
    ]


async def _collect_calendar(creds, tz_name: str) -> list[dict]:
    from tools.api_implementations.calendar_api import calendar_list_events

    start, end = _day_bounds(tz_name)
    result = await calendar_list_events(
        creds,
        time_min=start.isoformat(),
        time_max=end.isoformat(),
        max_results=20,
    )
    return [
        {
            "summary": e.get("summary", "(bez naslova)"),
            "start": e.get("start", ""),
            "location": e.get("location", ""),
        }
        for e in result.get("events", [])
    ]


async def _collect_tasks(creds, tz_name: str) -> list[dict]:
    from tools.api_implementations.tasks_api import tasks_list_tasks

    result = await tasks_list_tasks(creds, show_completed=False, max_results=50)
    _, end_of_day = _day_bounds(tz_name)
    out = []
    for task in result.get("tasks", []):
        due_raw = task.get("due")
        if not due_raw:
            continue
        try:
            due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if due < end_of_day:
            start_of_day, _ = _day_bounds(tz_name)
            out.append({
                "title": task.get("title", "(bez naslova)"),
                "due": due_raw,
                "overdue": due < start_of_day,
            })
    return out


async def _collect_weather(ctx: UserContext) -> Optional[str]:
    from services.voice_weather import _fetch_json, _geocode, format_weather_report
    from services.voice_weather import FORECAST_URL

    default_location = (
        os.getenv("VOICE_WEATHER_DEFAULT_LOCATION", "Zagreb").strip() or "Zagreb"
    )
    place = await _geocode(default_location)
    if place is None:
        return None
    forecast = await _fetch_json(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 1,
        },
    )
    return format_weather_report(
        place["name"], 0, forecast.get("current") or {}, forecast.get("daily") or {},
    )


async def _collect_home() -> Optional[dict]:
    if not os.getenv("MQTT_BROKER", "").strip():
        return None
    from tools.adk_tools.mqtt_adk_tools import mqtt_get_status

    status = await mqtt_get_status()
    # Broker reachable but zero devices reported = state UNKNOWN — reporting
    # "sve je ugašeno" from an empty snapshot would be a lie.
    if status.get("status") != "ok" or not status.get("total_devices"):
        return None
    lights_on = [n for n, v in status.get("lights", {}).items() if v == "ON"]
    outlets_on = [n for n, v in status.get("outlets", {}).items() if v == "ON"]
    return {"lights_on": lights_on, "outlets_on": outlets_on}


async def collect_briefing_data(ctx: Optional[UserContext] = None) -> dict:
    """Gather all briefing sections; failed sections become None."""
    ctx = ctx or get_default_user_context()
    creds = _get_credentials()
    data: dict = {
        "date": datetime.now(ZoneInfo(ctx.timezone)),
        "emails": None,
        "calendar": None,
        "tasks": None,
        "weather": None,
        "home": None,
    }

    async def _safe(name, coro):
        try:
            data[name] = await coro
        except Exception as e:
            logger.warning(f"Briefing section '{name}' failed: {e}")
            data[name] = None

    section_coros = []
    if creds is not None:
        section_coros.extend([
            _safe("emails", _collect_emails(creds)),
            _safe("calendar", _collect_calendar(creds, ctx.timezone)),
            _safe("tasks", _collect_tasks(creds, ctx.timezone)),
        ])
    section_coros.extend([
        _safe("weather", _collect_weather(ctx)),
        _safe("home", _collect_home()),
    ])
    await asyncio.gather(*section_coros)
    return data


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_briefing_fallback(data: dict, ctx: Optional[UserContext] = None) -> str:
    """Deterministic Croatian briefing — the guaranteed output path."""
    now = data.get("date") or datetime.now()
    day_name = _HR_WEEKDAYS[now.weekday()]
    lines = [f"Dnevni pregled za {day_name}, {now.day}. {now.month}. {now.year}."]

    weather = data.get("weather")
    lines.append(f"Vrijeme: {weather}" if weather else "Vrijeme: nedostupno.")

    calendar = data.get("calendar")
    if calendar is None:
        lines.append("Kalendar: nedostupan.")
    elif not calendar:
        lines.append("Kalendar: danas nema sastanaka.")
    else:
        lines.append(f"Kalendar: {len(calendar)} događaj(a) danas:")
        for event in calendar[:5]:
            start = event.get("start", "")
            time_part = start[11:16] if len(start) >= 16 else ""
            location = f" ({event['location']})" if event.get("location") else ""
            lines.append(f"  - {time_part} {event['summary']}{location}".rstrip())

    tasks = data.get("tasks")
    if tasks is None:
        lines.append("Zadaci: nedostupni.")
    elif not tasks:
        lines.append("Zadaci: nema zadataka s današnjim rokom.")
    else:
        overdue = [t for t in tasks if t.get("overdue")]
        today = [t for t in tasks if not t.get("overdue")]
        if overdue:
            lines.append(f"Zakašnjeli zadaci ({len(overdue)}):")
            lines.extend(f"  - {t['title']}" for t in overdue[:5])
        if today:
            lines.append(f"Zadaci za danas ({len(today)}):")
            lines.extend(f"  - {t['title']}" for t in today[:5])

    emails = data.get("emails")
    if emails is None:
        lines.append("Email: nedostupan.")
    elif not emails:
        lines.append("Email: nema nepročitanih poruka u zadnja 24 sata.")
    else:
        lines.append(f"Email: {len(emails)} nepročitanih u zadnja 24 sata:")
        for mail in emails[:5]:
            sender = mail.get("from", "").split("<")[0].strip() or mail.get("from", "")
            lines.append(f"  - {sender}: {mail['subject']}")

    home = data.get("home")
    if home is None:
        lines.append("Kuća: stanje nije dostupno.")
    else:
        on_count = len(home.get("lights_on", [])) + len(home.get("outlets_on", []))
        if on_count:
            names = ", ".join((home.get("lights_on", []) + home.get("outlets_on", []))[:6])
            lines.append(f"Kuća: uključeno {on_count} uređaja ({names}).")
        else:
            # A snapshot only covers devices that reported — avoid the
            # absolute claim "sve je ugašeno" from a possibly partial view.
            lines.append("Kuća: nijedan od javljenih uređaja nije uključen.")

    return "\n".join(lines)


async def summarize_briefing(data: dict, ctx: Optional[UserContext] = None) -> str:
    """One tool-less LLM pass over the collected data; fallback on any error."""
    fallback = format_briefing_fallback(data, ctx)
    if os.getenv("BRIEFING_LLM_SUMMARY", "true").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return fallback
    try:
        from agents.adk_agents.adk_agent_factory import create_adk_agent
        from agents.adk_agents.runner_utils import run_agent_simple

        agent = create_adk_agent(
            name="briefing_summarizer",
            model=os.getenv("BRIEFING_SUMMARY_MODEL", "gemini-3.5-flash"),
            instruction=(
                "Dobit ćeš strukturirani dnevni pregled. Prepričaj ga na hrvatskom "
                "kao kratak, prirodan jutarnji brifing (najviše 10 rečenica). "
                "Koristi ISKLJUČIVO podatke iz pregleda — ništa ne izmišljaj i ne "
                "dodaji. Zadrži sve brojke, imena i vremena. Sekcije označene kao "
                "'nedostupno' spomeni jednom rečenicom."
            ),
            description="Summarizes the daily briefing",
            load_instruction_from_file=False,
        )
        summary = await run_agent_simple(
            agent, fallback, session_id="briefing", user_id="briefing",
        )
        return summary.strip() or fallback
    except Exception as e:
        logger.warning(f"Briefing LLM summary failed, using fallback: {e}")
        return fallback


async def get_daily_briefing(ctx: Optional[UserContext] = None) -> str:
    """Public entry point: collect + summarize (with deterministic fallback)."""
    ctx = ctx or get_default_user_context()
    data = await collect_briefing_data(ctx)
    return await summarize_briefing(data, ctx)
