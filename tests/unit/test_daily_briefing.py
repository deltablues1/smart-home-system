"""
Unit tests for the daily briefing service and its voice route.

Collectors are mocked — no Google APIs, no MQTT, no LLM.

Run with:
    pytest tests/unit/test_daily_briefing.py -v
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import services.daily_briefing as daily_briefing
from config.user_context import get_default_user_context
from interfaces.base_interface import (
    BRIEFING_VOICE_ROUTE,
    WEATHER_VOICE_ROUTE,
    BaseInterface,
)


class _StubSystem:
    philosophy_keywords = {"filozofij", "sokrat"}


class _VoiceInterface(BaseInterface):
    def __init__(self):
        super().__init__(session_prefix="test")
        self.system = _StubSystem()

    async def start(self) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    def format_response(self, response: str) -> str:  # pragma: no cover - unused
        return response


@pytest.fixture
def iface():
    return _VoiceInterface()


class TestBriefingRoute:
    @pytest.mark.parametrize("text", [
        "Dnevni pregled",
        "Daj mi dnevni izvještaj",
        "Jutarnji pregled molim",
        "Što me čeka danas",
    ])
    def test_briefing_phrases_route(self, iface, text):
        route_type, _ = iface._classify_voice_route(text)
        assert route_type == BRIEFING_VOICE_ROUTE

    def test_weather_still_weather(self, iface):
        route_type, _ = iface._classify_voice_route("Kakvo je vrijeme sutra")
        assert route_type == WEATHER_VOICE_ROUTE

    def test_ordinary_question_not_briefing(self, iface):
        route_type, target = iface._classify_voice_route("Koliko je visok Mont Everest")
        assert route_type == "agent"


class TestCollect:
    def _patch_collectors(self, monkeypatch, *, emails=None, calendar=None,
                          tasks=None, weather=None, home=None,
                          email_raises=False):
        monkeypatch.setattr(daily_briefing, "_get_credentials", lambda: object())

        async def fake_emails(creds):
            if email_raises:
                raise RuntimeError("gmail down")
            return emails or []

        async def fake_calendar(creds, tz):
            return calendar or []

        async def fake_tasks(creds, tz):
            return tasks or []

        async def fake_weather(ctx):
            return weather

        async def fake_home():
            return home

        monkeypatch.setattr(daily_briefing, "_collect_emails", fake_emails)
        monkeypatch.setattr(daily_briefing, "_collect_calendar", fake_calendar)
        monkeypatch.setattr(daily_briefing, "_collect_tasks", fake_tasks)
        monkeypatch.setattr(daily_briefing, "_collect_weather", fake_weather)
        monkeypatch.setattr(daily_briefing, "_collect_home", fake_home)

    def test_happy_path(self, monkeypatch):
        self._patch_collectors(
            monkeypatch,
            emails=[{"from": "Ana <ana@x.com>", "subject": "Ponuda"}],
            calendar=[{"summary": "Sastanak", "start": "2026-07-11T10:00:00+02:00",
                       "location": "Ured"}],
            tasks=[{"title": "Platiti račun", "due": "2026-07-11", "overdue": False}],
            weather="U mjestu Zagreb trenutno je 25 stupnjeva i vedro.",
            home={"lights_on": ["Kuhinja"], "outlets_on": []},
        )

        data = asyncio.run(daily_briefing.collect_briefing_data())
        assert data["emails"][0]["subject"] == "Ponuda"
        assert data["calendar"][0]["summary"] == "Sastanak"
        assert data["weather"].startswith("U mjestu Zagreb")

    def test_dead_section_does_not_break_briefing(self, monkeypatch):
        self._patch_collectors(
            monkeypatch,
            email_raises=True,
            weather="Vedro.",
        )

        data = asyncio.run(daily_briefing.collect_briefing_data())
        assert data["emails"] is None
        assert data["weather"] == "Vedro."

    def test_no_credentials_skips_google_sections(self, monkeypatch):
        self._patch_collectors(monkeypatch, weather="Vedro.")
        monkeypatch.setattr(daily_briefing, "_get_credentials", lambda: None)

        data = asyncio.run(daily_briefing.collect_briefing_data())
        assert data["emails"] is None
        assert data["calendar"] is None
        assert data["weather"] == "Vedro."


class TestFallbackFormatter:
    def _sample_data(self):
        return {
            "date": datetime(2026, 7, 11, 7, 0, tzinfo=ZoneInfo("Europe/Zagreb")),
            "weather": "U mjestu Zagreb trenutno je 25 stupnjeva i vedro.",
            "calendar": [
                {"summary": "Sastanak s mentorom",
                 "start": "2026-07-11T10:00:00+02:00", "location": "FER"},
            ],
            "tasks": [
                {"title": "Predati poglavlje", "due": "2026-07-10", "overdue": True},
                {"title": "Platiti račun", "due": "2026-07-11", "overdue": False},
            ],
            "emails": [
                {"from": "Mentor <m@fer.hr>", "subject": "Komentari na rad"},
            ],
            "home": {"lights_on": ["Kuhinja"], "outlets_on": ["Utičnica TV"]},
        }

    def test_full_briefing_croatian(self):
        text = daily_briefing.format_briefing_fallback(self._sample_data())
        assert "Dnevni pregled za subota, 11. 7. 2026." in text
        assert "25 stupnjeva i vedro" in text
        assert "10:00 Sastanak s mentorom (FER)" in text
        assert "Zakašnjeli zadaci (1):" in text
        assert "Predati poglavlje" in text
        assert "Mentor: Komentari na rad" in text
        assert "uključeno 2 uređaja" in text

    def test_unavailable_sections_marked(self):
        data = self._sample_data()
        data["emails"] = None
        data["calendar"] = None
        text = daily_briefing.format_briefing_fallback(data)
        assert "Email: nedostupan." in text
        assert "Kalendar: nedostupan." in text

    def test_empty_sections(self):
        data = self._sample_data()
        data["emails"] = []
        data["calendar"] = []
        data["tasks"] = []
        text = daily_briefing.format_briefing_fallback(data)
        assert "danas nema sastanaka" in text
        assert "nema nepročitanih poruka" in text
        assert "nema zadataka" in text


class TestSummarize:
    def test_llm_disabled_returns_fallback(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_LLM_SUMMARY", "false")
        data = {"date": datetime(2026, 7, 11), "weather": "Vedro.",
                "calendar": None, "tasks": None, "emails": None, "home": None}
        result = asyncio.run(daily_briefing.summarize_briefing(data))
        assert result == daily_briefing.format_briefing_fallback(data)

    def test_llm_failure_returns_fallback(self, monkeypatch):
        monkeypatch.setenv("BRIEFING_LLM_SUMMARY", "true")

        def boom(*args, **kwargs):
            raise RuntimeError("no model")

        import agents.adk_agents.adk_agent_factory as factory
        monkeypatch.setattr(factory, "create_adk_agent", boom)

        data = {"date": datetime(2026, 7, 11), "weather": "Vedro.",
                "calendar": None, "tasks": None, "emails": None, "home": None}
        result = asyncio.run(daily_briefing.summarize_briefing(data))
        assert result == daily_briefing.format_briefing_fallback(data)


class TestSchedulerBriefingJob:
    def test_briefing_action_type_default_nl(self):
        from config.scheduler_config import JobTrigger, ScheduledJob

        job = ScheduledJob(
            id="j1", name="Test", agent_request="x",
            trigger=JobTrigger(type="cron", cron_expression="0 7 * * *"),
        )
        assert job.action_type == "nl"

        briefing_job = ScheduledJob(
            id="j2", name="Briefing", agent_request="",
            trigger=JobTrigger(type="cron", cron_expression="0 7 * * *"),
            action_type="briefing",
        )
        assert briefing_job.action_type == "briefing"
