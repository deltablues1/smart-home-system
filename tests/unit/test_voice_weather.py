"""
Unit tests for the voice weather lane: weather-vs-time disambiguation of the
Croatian word "vrijeme" in _classify_voice_route, plus the pure parsing and
formatting helpers in services.voice_weather.

Pure routing/formatting logic — no network, no LLM.

Run with:
    pytest tests/unit/test_voice_weather.py -v
"""

import asyncio

import pytest

import services.voice_weather as voice_weather
from interfaces.base_interface import (
    BaseInterface,
    HR_WEEKDAY_NAMES,
    LOCAL_VOICE_ROUTE,
    ORCHESTRATOR_VOICE_ROUTE,
    WEATHER_VOICE_ROUTE,
)
from services.voice_weather import (
    degrees_phrase,
    describe_wmo,
    detect_day_offset,
    extract_location,
    format_weather_report,
)


class _StubSystem:
    philosophy_keywords = {"filozofij", "sokrat"}


class _VoiceInterface(BaseInterface):
    """Minimal concrete BaseInterface for routing tests."""

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


class TestWeatherClassification:
    """The RPi incident: "kakvo je vrijeme" must never get a clock answer."""

    def test_weather_question_routes_to_weather(self, iface):
        assert iface._classify_voice_route(
            "kakvo je vrijeme sutra"
        ) == (WEATHER_VOICE_ROUTE, None)

    def test_forecast_with_city_routes_to_weather(self, iface):
        assert iface._classify_voice_route(
            "vremenska prognoza za Zagreb"
        ) == (WEATHER_VOICE_ROUTE, None)

    def test_rain_question_routes_to_weather(self, iface):
        assert iface._classify_voice_route(
            "hoće li kiša danas"
        ) == (WEATHER_VOICE_ROUTE, None)

    def test_temperature_question_routes_to_weather(self, iface):
        assert iface._classify_voice_route(
            "kolika je temperatura vani"
        ) == (WEATHER_VOICE_ROUTE, None)

    def test_sunny_weekend_routes_to_weather(self, iface):
        assert iface._classify_voice_route(
            "hoće li biti sunčano za vikend"
        ) == (WEATHER_VOICE_ROUTE, None)

    def test_business_keywords_win_over_weather(self, iface):
        # Mixed requests need tools; the orchestrator check stays first.
        route_type, _ = iface._classify_voice_route(
            "pošalji mi mail s vremenskom prognozom"
        )
        assert route_type == ORCHESTRATOR_VOICE_ROUTE

    def test_kisik_is_not_rain(self, iface):
        # Word-boundary guard: "kisik" contains "kisi" as a substring.
        assert iface._classify_voice_route(
            "trebam kisik za varenje"
        ) == ("agent", "voice_qa")

    def test_smart_home_lane_unaffected(self, iface):
        assert iface._classify_voice_route(
            "upali svjetlo na terasi"
        ) == ("agent", "smart_home")


class TestTimeDateClassification:

    def test_clock_question_stays_local(self, iface):
        assert iface._classify_voice_route(
            "koliko je sati"
        ) == (LOCAL_VOICE_ROUTE, None)

    def test_date_question_stays_local(self, iface):
        assert iface._classify_voice_route(
            "koji je datum"
        ) == (LOCAL_VOICE_ROUTE, None)

    def test_date_question_with_danas_stays_local(self, iface):
        assert iface._classify_voice_route(
            "koji je danas datum"
        ) == (LOCAL_VOICE_ROUTE, None)

    def test_prefixed_clock_question_stays_local(self, iface):
        assert iface._classify_voice_route(
            "reci mi koliko je sati"
        ) == (LOCAL_VOICE_ROUTE, None)

    def test_knowledge_date_question_is_not_local(self, iface):
        # "koji je datum rođenja..." is a knowledge question; a local
        # "Danas je..." answer would be nonsense.
        route = iface._classify_voice_route("koji je datum rođenja pape Leona")
        assert route != (LOCAL_VOICE_ROUTE, None)

    def test_bare_vrijeme_no_longer_matches_time(self, iface):
        # The original bug: "vrijeme" as a time keyword hijacked weather.
        assert not iface._is_time_or_date_request("kakvo je vrijeme sutra")


class TestTimeDateResponse:

    def test_date_answer_speaks_croatian_weekday(self, iface):
        response = iface._build_time_or_date_response("koji je danas datum")
        assert response.startswith("Danas je ")
        assert any(day in response for day in HR_WEEKDAY_NAMES)
        # strftime %A regression: no English weekday through TTS.
        english_days = (
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        )
        assert not any(day in response for day in english_days)

    def test_clock_answer_unchanged(self, iface):
        assert iface._build_time_or_date_response("koliko je sati").startswith("Trenutno je ")


class TestWeatherHelpers:

    def test_describe_wmo(self):
        assert describe_wmo(0) == "vedro"
        assert describe_wmo(63) == "kiša"
        assert describe_wmo(None) == "promjenjivo"
        assert describe_wmo(42) == "promjenjivo"

    def test_degrees_phrase_croatian_grammar(self):
        assert degrees_phrase(1) == "1 stupanj"
        assert degrees_phrase(23.6) == "24 stupnja"
        assert degrees_phrase(11) == "11 stupnjeva"
        assert degrees_phrase(21) == "21 stupanj"
        assert degrees_phrase(5) == "5 stupnjeva"
        assert degrees_phrase(-5.2) == "minus 5 stupnjeva"

    def test_detect_day_offset(self):
        # "prekosutra" contains "sutra" — order of checks matters.
        assert detect_day_offset("kakvo je vrijeme prekosutra") == 2
        assert detect_day_offset("kakvo je vrijeme sutra") == 1
        assert detect_day_offset("hoće li kiša") == 0

    def test_extract_location(self):
        assert extract_location("vremenska prognoza za Zagreb") == "zagreb"
        assert extract_location("kakvo je vrijeme sutra u Splitu") == "splitu"
        assert extract_location("kakva je prognoza za Novi Vinodolski") == "novi vinodolski"

    def test_nominative_candidates_for_known_locatives(self):
        # "Rijeci" fuzzy-geocodes to Rečica without the explicit map.
        assert voice_weather.nominative_candidates("rijeci") == ["rijeka"]
        assert voice_weather.nominative_candidates("zagrebu") == ["zagreb"]

    def test_nominative_candidates_fallback_trims_vowel(self):
        assert voice_weather.nominative_candidates("kutini") == ["kutini", "kutin"]

    def test_extract_location_ignores_day_words(self):
        assert extract_location("hoće li kiša danas") is None
        assert extract_location("kakvo će biti vrijeme za vikend") is None
        assert extract_location("kakvo je vrijeme u Zagrebu danas") == "zagrebu"


class TestWeatherFormatting:

    CURRENT = {"temperature_2m": 23.6, "weather_code": 0, "wind_speed_10m": 10.0}
    DAILY = {
        "weather_code": [0, 63, 3],
        "temperature_2m_max": [28.2, 22.1, 25.0],
        "temperature_2m_min": [17.0, 15.4, 16.0],
        "precipitation_probability_max": [10, 80, 30],
    }

    def test_today_report(self):
        report = format_weather_report("Zagreb", 0, self.CURRENT, self.DAILY)
        assert "U mjestu Zagreb trenutno je 24 stupnja i vedro." in report
        assert "najviša 28 stupnjeva" in report
        assert "Vjerojatnost oborina 10 posto." in report
        # 10 km/h is not worth mentioning.
        assert "vjetar" not in report

    def test_tomorrow_report(self):
        report = format_weather_report("Zagreb", 1, self.CURRENT, self.DAILY)
        assert report.startswith("Sutra u mjestu Zagreb: kiša")
        assert "najviša 22 stupnja" in report
        assert "Vjerojatnost oborina 80 posto." in report

    def test_strong_wind_is_mentioned_today(self):
        current = dict(self.CURRENT, wind_speed_10m=42.0)
        report = format_weather_report("Zagreb", 0, current, self.DAILY)
        assert "jak vjetar" in report
        assert "42 kilometara na sat" in report

    def test_missing_precipitation_is_omitted(self):
        daily = dict(self.DAILY, precipitation_probability_max=None)
        report = format_weather_report("Zagreb", 1, self.CURRENT, daily)
        assert "oborina" not in report


class TestWeatherFailurePath:

    def test_network_failure_returns_none(self, monkeypatch):
        async def _boom(url, params):
            raise OSError("network down")

        monkeypatch.setattr(voice_weather, "_fetch_json", _boom)
        result = asyncio.run(voice_weather.get_voice_weather_report("kakvo je vrijeme sutra"))
        assert result is None
