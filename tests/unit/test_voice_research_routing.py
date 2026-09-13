"""A device noun is not a device command.

"bojler", "terasa", "kuhinja", "kanal" and "program" are ordinary Croatian as
well as things in this house, and SMART_HOME_KEYWORDS matches them as
substrings anywhere in the sentence. Measured 2026-09-06 on the Pi: "istrazi
detaljno prednosti toplinskih pumpi zrak-voda u odnosu na plinski bojler" was
routed straight to smart_home, which answered "to je izvan mojih mogucnosti" in
two seconds. Nobody was asking about the boiler in the bathroom.

The fix sits where the weather check sits: after the business keywords, so a
mixed request ("posalji mail s usporedbom") still goes the business way, and
before every agent lane.

Run with:
    pytest tests/unit/test_voice_research_routing.py -v
"""

import pytest

from interfaces.base_interface import (
    ORCHESTRATOR_VOICE_ROUTE,
    RESEARCH_VOICE_KEYWORDS,
    BaseInterface,
)


@pytest.fixture
def iface():
    from types import SimpleNamespace

    class _Iface(BaseInterface):
        def __init__(self):
            super().__init__(session_prefix="test")
            self.system = SimpleNamespace(philosophy_keywords=set())

        async def start(self):  # pragma: no cover
            pass

        async def stop(self):  # pragma: no cover
            pass

        def format_response(self, response):  # pragma: no cover
            return response

    return _Iface()


class TestTheReportedFailure:
    def test_the_exact_message_no_longer_reaches_smart_home(self, iface):
        route, target = iface._classify_voice_route(
            "Istrazi detaljno koje su prednosti i nedostaci toplinskih pumpi "
            "zrak-voda u odnosu na plinski bojler za obiteljsku kucu u Hrvatskoj."
        )
        assert target != "smart_home"
        assert route == ORCHESTRATOR_VOICE_ROUTE

    @pytest.mark.parametrize(
        "message",
        [
            "Isplati li se toplinska pumpa umjesto plinskog bojlera?",
            "Usporedi cijene bojlera i toplinske pumpe.",
            "Koliko kosta novi bojler za kupaonu?",
            "Koje su prednosti LED rasvjete u kuhinji?",
            "Sto je bolje, plinski ili elektricni bojler?",
        ],
    )
    def test_questions_that_merely_name_a_device(self, iface, message):
        _, target = iface._classify_voice_route(message)
        assert target != "smart_home", f"{message!r} is a question, not a command"


class TestRealCommandsAreUntouched:
    """The fast path is the point of this router; it must not slow down."""

    @pytest.mark.parametrize(
        "message",
        [
            "Upali svjetlo u kuhinji",
            "Ugasi bojler",
            "Ukljuci uticnicu na terasi",
            "Upali televizor",
            "Pojacaj glasnocu",
            "Ugasi svjetlo u hodniku",
            "Scena film",
        ],
    )
    def test_commands_still_take_the_device_lane(self, iface, message):
        _, target = iface._classify_voice_route(message)
        assert target == "smart_home", f"{message!r} must stay on the fast path"


class TestOrderingAgainstTheOtherLanes:
    def test_a_business_request_still_wins(self, iface):
        """"posalji mail s usporedbom" is business first, exactly like weather."""
        route, _ = iface._classify_voice_route(
            "Posalji mail s usporedbom bojlera i toplinske pumpe."
        )
        assert route == ORCHESTRATOR_VOICE_ROUTE

    def test_an_ordinary_question_is_not_swept_up(self, iface):
        _, target = iface._classify_voice_route("Sto je fotosinteza?")
        assert target == "voice_qa"


class TestTheKeywordSetItself:
    def test_stems_are_ascii_folded(self):
        """They are matched against _normalize_voice_text output."""
        for kw in RESEARCH_VOICE_KEYWORDS:
            assert kw == kw.lower()
            assert all(ord(ch) < 128 for ch in kw), f"{kw!r} would never match"

    def test_it_stays_tight(self):
        """Over-routing costs a slow answer; this set must not creep."""
        assert len(RESEARCH_VOICE_KEYWORDS) <= 15


class TestShoppingListReachesTheHouseAgent:
    """The list is an HA todo entity, so smart_home owns it.

    Nothing in SMART_HOME_KEYWORDS matched a shopping phrase, so the voice
    lane would have dropped these into voice_qa -- which has no tools and
    would have answered as though it had written something down.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Stavi ulje i brasno na listu za kupovinu",
            "Dodaj mlijeko na popis",
            "Sto trebam kupiti?",
            "Kupio sam sve osim mlijeka",
            "Makni kruh s liste",
            # What the user actually said on 2026-09-06; the HA entity is
            # named "Shopping List", so the English word is the natural one.
            "Dodaj mi na shopping listu ulje, kruh i mlijeko",
            "Stavi na shoping listu kruh",
        ],
    )
    def test_shopping_phrases_route_to_smart_home(self, iface, message):
        _, target = iface._classify_voice_route(message)
        assert target == "smart_home", f"{message!r} would reach an agent with no list"

    def test_a_task_list_still_goes_to_the_orchestrator(self, iface):
        """"lista zadataka" is Google Tasks, not the shopping list."""
        route, target = iface._classify_voice_route("Dodaj na listu zadataka nazvati Antu")
        assert target != "smart_home"
        assert route == ORCHESTRATOR_VOICE_ROUTE
