"""Pure meeting-slot computation — no I/O, fully unit-testable.

Takes the busy intervals returned by the Calendar FreeBusy API and produces
up to ``max_slots`` proposal slots inside working hours, plus a Croatian
spoken/written proposal. All datetimes are handled timezone-aware in the
user's zone.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

HR_WEEKDAY_NAMES = (
    "ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja",
)


def _parse(value: str, tz: ZoneInfo) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def merge_busy_intervals(
    busy_map: Dict[str, List[dict]],
    tz: ZoneInfo,
) -> List[Tuple[datetime, datetime]]:
    """Flatten everyone's busy blocks into one sorted, merged interval list."""
    intervals: List[Tuple[datetime, datetime]] = []
    for blocks in busy_map.values():
        for block in blocks:
            start = _parse(block.get("start", ""), tz)
            end = _parse(block.get("end", ""), tz)
            if start and end and end > start:
                intervals.append((start, end))
    intervals.sort()

    merged: List[Tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def compute_free_slots(
    busy_map: Dict[str, List[dict]],
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    working_hours: Tuple[int, int] = (9, 17),
    tz_name: str = "Europe/Zagreb",
    max_slots: int = 3,
    step_minutes: int = 30,
) -> List[Tuple[datetime, datetime]]:
    """Find up to max_slots meeting slots free for ALL queried calendars.

    Args:
        busy_map: {email: [{"start": rfc3339, "end": rfc3339}, ...]} — only
            calendars whose availability is actually known (callers must
            handle "unknown" attendees separately).
        window_start/window_end: search window (aware or naive; naive is
            interpreted in tz_name)
        duration_minutes: meeting length
        working_hours: (start_hour, end_hour) local time; weekends skipped
        step_minutes: candidate start-time granularity
    """
    tz = ZoneInfo(tz_name)
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=tz)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=tz)
    window_start = window_start.astimezone(tz)
    window_end = window_end.astimezone(tz)

    busy = merge_busy_intervals(busy_map, tz)
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    work_start, work_end = working_hours

    slots: List[Tuple[datetime, datetime]] = []
    day = window_start.replace(hour=0, minute=0, second=0, microsecond=0)

    while day < window_end and len(slots) < max_slots:
        if day.weekday() < 5:  # Monday..Friday
            candidate = max(
                day.replace(hour=work_start),
                window_start,
            )
            # Align to the step grid
            offset = (candidate.minute % step_minutes)
            if offset or candidate.second or candidate.microsecond:
                candidate += timedelta(
                    minutes=step_minutes - offset,
                    seconds=-candidate.second,
                    microseconds=-candidate.microsecond,
                )
            day_end = day.replace(hour=work_end)

            while candidate + duration <= min(day_end, window_end):
                slot_end = candidate + duration
                conflict = next(
                    (b for b in busy if b[0] < slot_end and candidate < b[1]),
                    None,
                )
                if conflict is None:
                    slots.append((candidate, slot_end))
                    if len(slots) >= max_slots:
                        break
                    candidate = slot_end
                else:
                    # Jump past the conflicting block, back onto the grid
                    candidate = conflict[1]
                    offset = candidate.minute % step_minutes
                    if offset or candidate.second or candidate.microsecond:
                        candidate += timedelta(
                            minutes=step_minutes - offset,
                            seconds=-candidate.second,
                            microseconds=-candidate.microsecond,
                        )
        day += timedelta(days=1)

    return slots


def format_slot_proposal_hr(slots: List[Tuple[datetime, datetime]]) -> str:
    """Croatian numbered proposal of meeting slots."""
    if not slots:
        return "Nažalost, nema slobodnih termina u traženom razdoblju."
    lines = ["Predlažem sljedeće termine:"]
    for i, (start, end) in enumerate(slots, 1):
        day_name = HR_WEEKDAY_NAMES[start.weekday()]
        lines.append(
            f"{i}. {day_name}, {start.day}. {start.month}. {start.year}. "
            f"od {start.strftime('%H:%M')} do {end.strftime('%H:%M')}"
        )
    lines.append("Koji ti odgovara?")
    return "\n".join(lines)
