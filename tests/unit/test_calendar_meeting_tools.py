"""
Unit tests for the meeting-lifecycle Calendar tools (Phase D):

- calendar_freebusy request body and unknown-attendee handling
- calendar_create_event with Meet link (conferenceDataVersion=1, requestId)
  and sendUpdates
- calendar_create_meeting / calendar_propose_meeting_slots ADK wrappers

All Google APIs mocked — no network.

Run with:
    pytest tests/unit/test_calendar_meeting_tools.py -v
"""

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_creds():
    creds = MagicMock()
    creds.valid = True
    return creds


def _mock_calendar_service(monkeypatch):
    import tools.api_implementations.calendar_api as calendar_api

    service = MagicMock()
    api_client = MagicMock()
    api_client.calendar_service.return_value = service
    monkeypatch.setattr(
        "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
    )
    return service, calendar_api


class TestFreeBusy:
    def test_request_body_and_unknown_split(self, monkeypatch, fake_creds):
        service, calendar_api = _mock_calendar_service(monkeypatch)

        async def fake_aexecute(request):
            return {
                "calendars": {
                    "ana@x.com": {"busy": [
                        {"start": "2026-07-13T09:00:00+02:00",
                         "end": "2026-07-13T10:00:00+02:00"},
                    ]},
                    "vanjski@firma.hr": {"errors": [{"reason": "notFound"}]},
                }
            }

        monkeypatch.setattr(calendar_api, "aexecute", fake_aexecute)

        result = asyncio.run(calendar_api.calendar_freebusy(
            fake_creds,
            "2026-07-13T00:00:00+02:00",
            "2026-07-18T00:00:00+02:00",
            ["ana@x.com", "vanjski@firma.hr"],
        ))

        body = service.freebusy().query.call_args.kwargs["body"]
        assert body["timeMin"] == "2026-07-13T00:00:00+02:00"
        assert body["timeZone"] == "Europe/Zagreb"
        assert {"id": "ana@x.com"} in body["items"]

        assert result["unknown"] == ["vanjski@firma.hr"]
        assert len(result["busy"]["ana@x.com"]) == 1


class TestCreateEventMeetLink:
    def _created_event(self):
        return {
            "id": "evt1", "summary": "Sastanak",
            "start": {"dateTime": "2026-07-13T10:00:00+02:00"},
            "end": {"dateTime": "2026-07-13T11:00:00+02:00"},
            "htmlLink": "https://cal/evt1",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "attendees": [{"email": "ana@x.com"}],
        }

    def test_meet_link_and_send_updates(self, monkeypatch, fake_creds):
        service, calendar_api = _mock_calendar_service(monkeypatch)

        async def fake_aexecute(request):
            return self._created_event()

        monkeypatch.setattr(calendar_api, "aexecute", fake_aexecute)

        result = asyncio.run(calendar_api.calendar_create_event(
            fake_creds, "Sastanak",
            "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            attendees=["ana@x.com"],
            add_meet_link=True,
            send_updates="all",
        ))

        kwargs = service.events().insert.call_args.kwargs
        assert kwargs["conferenceDataVersion"] == 1
        assert kwargs["sendUpdates"] == "all"
        conference = kwargs["body"]["conferenceData"]["createRequest"]
        assert conference["conferenceSolutionKey"]["type"] == "hangoutsMeet"
        assert len(conference["requestId"]) == 32  # uuid4 hex

        assert result["meet_link"] == "https://meet.google.com/abc-defg-hij"
        assert result["attendees"] == ["ana@x.com"]

    def test_no_meet_link_no_conference(self, monkeypatch, fake_creds):
        service, calendar_api = _mock_calendar_service(monkeypatch)

        async def fake_aexecute(request):
            event = self._created_event()
            event.pop("hangoutLink")
            return event

        monkeypatch.setattr(calendar_api, "aexecute", fake_aexecute)

        asyncio.run(calendar_api.calendar_create_event(
            fake_creds, "Sastanak",
            "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
        ))

        kwargs = service.events().insert.call_args.kwargs
        assert "conferenceDataVersion" not in kwargs
        assert "sendUpdates" not in kwargs
        assert "conferenceData" not in kwargs["body"]


class TestAdkWrappers:
    @pytest.fixture(autouse=True)
    def clean_proposals(self):
        from services import approvals

        approvals.reset()
        yield
        approvals.reset()

    def _fake_create(self, monkeypatch, fake_creds, captured):
        import tools.adk_tools.calendar_adk_tools as calendar_adk
        import tools.api_implementations.calendar_api as calendar_api

        async def fake_create(creds, summary, start_time, end_time,
                              description=None, location=None, attendees=None,
                              calendar_id="primary", timezone=None,
                              add_meet_link=False, send_updates=None):
            captured.update(dict(
                summary=summary, attendees=attendees,
                add_meet_link=add_meet_link, send_updates=send_updates,
            ))
            return {"id": "evt1", "status": "created",
                    "meet_link": "https://meet.google.com/x"}

        monkeypatch.setattr(calendar_api, "calendar_create_event", fake_create)
        monkeypatch.setattr(calendar_adk, "_get_credentials", lambda: fake_creds)
        return calendar_adk

    def test_create_meeting_requires_proposed_slot(self, monkeypatch, fake_creds):
        # No proposal registered -> the model cannot create a meeting on its own.
        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ana@x.com"],
        ))

        assert result["status"] == "needs_confirmation"
        assert captured == {}  # impl was never called

    def test_same_turn_propose_and_create_blocked(self, monkeypatch, fake_creds):
        # Slots are TURN-gated: proposing and creating in the same model turn
        # (no user message in between = no arm_pending_proposals) must fail.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        tz = ZoneInfo("Europe/Zagreb")
        slot = (datetime(2026, 7, 13, 10, 0, tzinfo=tz),
                datetime(2026, 7, 13, 11, 0, tzinfo=tz))
        calendar_adk._register_proposed_slots([slot])  # NOT armed yet

        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ana@x.com"],
        ))
        assert result["status"] == "needs_confirmation"
        assert captured == {}

    def test_create_meeting_with_armed_proposed_slot(self, monkeypatch, fake_creds):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        tz = ZoneInfo("Europe/Zagreb")
        slot = (datetime(2026, 7, 13, 10, 0, tzinfo=tz),
                datetime(2026, 7, 13, 11, 0, tzinfo=tz))
        # Proposed FOR these people: the guest list is part of what was shown.
        calendar_adk._register_proposed_slots([slot], ["ana@x.com"])
        calendar_adk.arm_pending_proposals()  # user replied in a new turn

        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ana@x.com"],
        ))

        assert captured["attendees"] == ["ana@x.com"]
        assert captured["add_meet_link"] is True
        assert captured["send_updates"] == "all"
        assert result["meet_link"]

    def test_the_guest_list_is_part_of_what_was_approved(self, monkeypatch, fake_creds):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        tz = ZoneInfo("Europe/Zagreb")
        slot = (datetime(2026, 7, 13, 10, 0, tzinfo=tz),
                datetime(2026, 7, 13, 11, 0, tzinfo=tz))
        calendar_adk._register_proposed_slots([slot], ["ana@x.com"])
        calendar_adk.arm_pending_proposals()

        # Same hour, different people. The key used to be start+end alone, so
        # approving a slot for Ana also created it for whoever came next.
        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ana@x.com", "sef@konkurencija.com"],
        ))

        assert result["status"] == "needs_confirmation"
        assert captured == {}

    def test_the_order_of_the_guest_list_does_not_matter(self, monkeypatch, fake_creds):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        tz = ZoneInfo("Europe/Zagreb")
        slot = (datetime(2026, 7, 13, 10, 0, tzinfo=tz),
                datetime(2026, 7, 13, 11, 0, tzinfo=tz))
        calendar_adk._register_proposed_slots([slot], ["Ana@x.com", "ivo@x.com"])
        calendar_adk.arm_pending_proposals()

        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ivo@x.com", "ana@x.com"],
        ))

        assert result.get("status") != "needs_confirmation"
        assert captured["attendees"] == ["ivo@x.com", "ana@x.com"]

        # consume-on-use: creating the SAME slot again is gated (no double create)
        repeat = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            ["ana@x.com"],
        ))
        assert repeat["status"] == "needs_confirmation"

    def test_create_meeting_custom_time_turn_gated(self, monkeypatch, fake_creds):
        # A user-dictated time also goes through the turn gate: first call
        # registers + asks, only after the user's next turn does it proceed.
        captured = {}
        calendar_adk = self._fake_create(monkeypatch, fake_creds, captured)

        first = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-15T14:00:00+02:00", "2026-07-15T15:00:00+02:00",
            ["ana@x.com"], user_confirmed_custom_time=True,
        ))
        assert first["status"] == "needs_confirmation"
        assert captured == {}

        calendar_adk.arm_pending_proposals()  # user confirmed in a new turn

        result = asyncio.run(calendar_adk.calendar_create_meeting(
            "Sastanak", "2026-07-15T14:00:00+02:00", "2026-07-15T15:00:00+02:00",
            ["ana@x.com"], user_confirmed_custom_time=True,
        ))
        assert result["meet_link"]
        assert captured["send_updates"] == "all"

    def test_generic_create_event_gates_attendees(self, monkeypatch, fake_creds):
        # calendar_create_event must not be a bypass of the meeting gate.
        import tools.adk_tools.calendar_adk_tools as calendar_adk

        monkeypatch.setattr(calendar_adk, "_get_credentials", lambda: fake_creds)

        result = asyncio.run(calendar_adk.calendar_create_event(
            "Sastanak", "2026-07-13T10:00:00+02:00", "2026-07-13T11:00:00+02:00",
            attendees=["ana@x.com"],
        ))
        assert result["status"] == "needs_confirmation"
        assert "calendar_create_meeting" in result["message"]

    def test_propose_slots_flags_unknown(self, monkeypatch, fake_creds):
        import tools.adk_tools.calendar_adk_tools as calendar_adk
        import tools.api_implementations.calendar_api as calendar_api

        async def fake_freebusy(creds, time_min, time_max, emails, timezone=None):
            return {"busy": {"primary": []}, "unknown": ["vanjski@firma.hr"]}

        monkeypatch.setattr(calendar_api, "calendar_freebusy", fake_freebusy)
        monkeypatch.setattr(calendar_adk, "_get_credentials", lambda: fake_creds)

        result = asyncio.run(calendar_adk.calendar_propose_meeting_slots(
            ["vanjski@firma.hr"], duration_minutes=60, window_days=5,
        ))

        assert result["status"] == "ok"
        assert result["unknown_availability"] == ["vanjski@firma.hr"]
        assert len(result["slots"]) <= 3
        assert "proposal" in result

    def test_meeting_request_routes_to_secretary(self):
        # "zakaži sastanak" must reach the secretary lane, not the
        # minutes-slow orchestrator (business keywords would otherwise win).
        from interfaces.base_interface import BaseInterface

        class _Iface(BaseInterface):
            def __init__(self):
                super().__init__(session_prefix="test")
                from types import SimpleNamespace
                self.system = SimpleNamespace(philosophy_keywords=set())

            async def start(self):  # pragma: no cover
                pass

            async def stop(self):  # pragma: no cover
                pass

            def format_response(self, response):  # pragma: no cover
                return response

        iface = _Iface()
        assert iface._classify_voice_route("Zakaži sastanak s Anom") == ("agent", "secretary")
        assert iface._classify_voice_route("Dogovori termin s mentorom") == ("agent", "secretary")
        # mixed multi-step request still goes to the orchestrator
        route_type, _ = iface._classify_voice_route(
            "Zakaži sastanak s Anom i pošalji mail s potvrdom"
        )
        assert route_type == "orchestrator"

    def test_secretary_agent_lane_pin(self):
        # "da" after a secretary turn must return to secretary, not voice_qa
        from interfaces.base_interface import BaseInterface

        class _Iface(BaseInterface):
            def __init__(self):
                super().__init__(session_prefix="test")

            async def start(self):  # pragma: no cover
                pass

            async def stop(self):  # pragma: no cover
                pass

            def format_response(self, response):  # pragma: no cover
                return response

        iface = _Iface()
        # First turn routes to secretary -> pin is set
        route = iface._apply_voice_lane_pin("s1", "zakaži sastanak s Anom", "agent", "secretary")
        assert route == ("agent", "secretary")
        # Short confirmation would otherwise fall into voice_qa
        route = iface._apply_voice_lane_pin("s1", "da, prvi termin", "agent", "voice_qa")
        assert route == ("agent", "secretary")
