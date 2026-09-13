"""A measurement question about the house goes to the house, not the forecast.

Measured 2026-09-13 on the Pi: "Kolika je sada temperatura u kupaoni?" asked
through Home Assistant came back as the Zagreb weather forecast, because
"temperatur" is a weather keyword and the weather lane is checked before the
device lanes.

Run with:
    pytest tests/unit/test_home_sensor_routing.py -v
"""

import pytest

from interfaces.base_interface import BaseInterface, WEATHER_VOICE_ROUTE


class _StubSystem:
    philosophy_keywords = {"filozofij", "sokrat"}


class _VoiceInterface(BaseInterface):
    def __init__(self):
        super().__init__(session_prefix="test")
        self.system = _StubSystem()

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    def format_response(self, response: str) -> str:  # pragma: no cover
        return response


@pytest.fixture
def iface():
    return _VoiceInterface()


@pytest.mark.parametrize("message", [
    "Kolika je sada temperatura u kupaoni?",
    "Kolika je temperatura u dnevnom boravku?",
    "Koliko je vlaga u sobi?",
    "Koliko je stupnjeva na katu?",
    "Kakav je zrak u kući?",
    "Koja je danas bila najviša vanjska temperatura?",
    "Koliko smo jučer potrošili struje?",
    "Kolika je potrošnja kuće sada?",
    "Ima li grešaka u Home Assistantu?",
])
def test_house_questions_go_to_smart_home(iface, message):
    assert iface._classify_voice_route(message) == ("agent", "smart_home")


@pytest.mark.parametrize("message", [
    "kolika je temperatura vani",
    "kakvo je vrijeme sutra",
    "kolika će biti temperatura sutra u sobi",
    "hoće li kiša danas",
    "vremenska prognoza za Zagreb",
])
def test_weather_questions_stay_on_the_weather_lane(iface, message):
    assert iface._classify_voice_route(message) == (WEATHER_VOICE_ROUTE, None)


def test_a_word_containing_soba_is_not_a_room(iface):
    assert not iface._looks_like_home_sensor_request("koja je temperatura tijela zdrave osobe")
