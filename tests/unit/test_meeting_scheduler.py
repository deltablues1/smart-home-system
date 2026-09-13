"""
Unit tests for the pure meeting-slot computation (services/meeting_scheduler.py).

No I/O — busy maps are plain dicts like the FreeBusy API returns.

Run with:
    pytest tests/unit/test_meeting_scheduler.py -v
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from services.meeting_scheduler import (
    compute_free_slots,
    format_slot_proposal_hr,
    merge_busy_intervals,
)

TZ = ZoneInfo("Europe/Zagreb")


def _dt(day, hour, minute=0):
    # July 2026: 13th = Monday, 14th = Tuesday ... 18th/19th = weekend
    return datetime(2026, 7, day, hour, minute, tzinfo=TZ)


def _busy(email, *blocks):
    return {
        email: [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in blocks
        ]
    }


class TestMergeBusy:
    def test_overlapping_intervals_merged(self):
        busy = {
            "a@x.com": [
                {"start": _dt(13, 9).isoformat(), "end": _dt(13, 11).isoformat()},
            ],
            "b@x.com": [
                {"start": _dt(13, 10).isoformat(), "end": _dt(13, 12).isoformat()},
            ],
        }
        merged = merge_busy_intervals(busy, TZ)
        assert len(merged) == 1
        assert merged[0] == (_dt(13, 9), _dt(13, 12))

    def test_invalid_entries_skipped(self):
        busy = {"a@x.com": [{"start": "garbage", "end": "also-garbage"}]}
        assert merge_busy_intervals(busy, TZ) == []


class TestComputeFreeSlots:
    def test_free_day_proposes_morning_slots(self):
        slots = compute_free_slots(
            {}, _dt(13, 8), _dt(14, 0), 60, max_slots=3,
        )
        assert len(slots) == 3
        assert slots[0] == (_dt(13, 9), _dt(13, 10))
        assert slots[1] == (_dt(13, 10), _dt(13, 11))

    def test_busy_morning_pushes_slots_after(self):
        busy = _busy("a@x.com", (_dt(13, 9), _dt(13, 12)))
        slots = compute_free_slots(busy, _dt(13, 8), _dt(14, 0), 60, max_slots=1)
        assert slots[0][0] == _dt(13, 12)

    def test_conflict_end_off_grid_realigned(self):
        busy = _busy("a@x.com", (_dt(13, 9), _dt(13, 9, 45)))
        slots = compute_free_slots(busy, _dt(13, 8), _dt(14, 0), 60, max_slots=1)
        # 9:45 realigned up to the 30-min grid -> 10:00
        assert slots[0][0] == _dt(13, 10)

    def test_weekend_skipped(self):
        # Window covering Sat 18.7. + Sun 19.7. + Mon 20.7.2026
        slots = compute_free_slots({}, _dt(18, 8), _dt(21, 0), 60, max_slots=1)
        assert slots[0][0].weekday() == 0  # Monday
        assert slots[0][0].day == 20

    def test_working_hours_respected(self):
        busy = _busy("a@x.com", (_dt(13, 9), _dt(13, 16, 30)))
        slots = compute_free_slots(
            busy, _dt(13, 8), _dt(15, 0), 60,
            working_hours=(9, 17), max_slots=1,
        )
        # Only 16:30-17:00 remains on Monday — too short for 60 min, so the
        # first slot must be on Tuesday morning.
        assert slots[0][0] == _dt(14, 9)

    def test_no_common_slot_returns_empty(self):
        busy = _busy("a@x.com", (_dt(13, 0), _dt(14, 0)))  # busy entire window
        slots = compute_free_slots(busy, _dt(13, 8), _dt(13, 23), 60)
        assert slots == []

    def test_naive_window_treated_as_local(self):
        slots = compute_free_slots(
            {}, datetime(2026, 7, 13, 8), datetime(2026, 7, 13, 23), 60, max_slots=1,
        )
        assert slots[0][0].hour == 9
        assert slots[0][0].tzinfo is not None


class TestFormatProposal:
    def test_croatian_numbered_proposal(self):
        slots = [(_dt(13, 9), _dt(13, 10)), (_dt(14, 11), _dt(14, 12))]
        text = format_slot_proposal_hr(slots)
        assert "1. ponedjeljak, 13. 7. 2026. od 09:00 do 10:00" in text
        assert "2. utorak, 14. 7. 2026. od 11:00 do 12:00" in text
        assert text.endswith("Koji ti odgovara?")

    def test_empty_slots_message(self):
        assert "nema slobodnih termina" in format_slot_proposal_hr([])
