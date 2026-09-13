"""A command aimed at later is not a command for now.

"Sutra ujutro u 7 upali TV i pusti neku pjesmu" names a device, so the voice
router handed it to smart_home, which answered -- honestly and uselessly --
that it cannot defer anything. Measured 2026-09-12 on something that had been
working. Only the orchestrator reaches the scheduler agent.

The discriminator needs both halves. A time marker alone catches "kolika je
bila temperatura ujutro", which is a sensor question the house agent should
keep; an action stem alone is just an ordinary command.

Run with:
    pytest tests/unit/test_voice_scheduled_routing.py -v
"""

from types import SimpleNamespace

import pytest

from interfaces.base_interface import ORCHESTRATOR_VOICE_ROUTE, BaseInterface


@pytest.fixture
def iface():
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
    def test_the_exact_request_reaches_the_orchestrator(self, iface):
        route, target = iface._classify_voice_route(
            "Sutra ujutro u 7 upali TV i pusti neku pjesmu"
        )
        assert target != "smart_home", "smart_home cannot defer and will say so"
        assert route == ORCHESTRATOR_VOICE_ROUTE


class TestDeferredCommands:
    @pytest.mark.parametrize(
        "message",
        [
            "Sutra ugasi bojler",
            "Prekosutra u 8 upali radio",
            "Svako jutro u 7 upali TV",
            "Svaki dan u 21 ugasi vanjsko svjetlo",
            "Radnim danima u 6:30 pusti vijesti",
            "Za 10 minuta ugasi svjetlo",
            "Za dva sata ugasi bojler",
            "U 21h upali vanjsko svjetlo",
            "Svaki ponedjeljak posalji izvjestaj",
        ],
    )
    def test_they_all_go_where_the_scheduler_lives(self, iface, message):
        route, _ = iface._classify_voice_route(message)
        assert route == ORCHESTRATOR_VOICE_ROUTE, message


class TestNothingElseIsSweptUp:
    @pytest.mark.parametrize(
        "message",
        [
            "Upali svjetlo u kuhinji",
            "Ugasi bojler",
            "Pojacaj glasnocu",
            "Upali televizor",
            "Scena film",
        ],
    )
    def test_plain_commands_keep_the_fast_path(self, iface, message):
        _, target = iface._classify_voice_route(message)
        assert target == "smart_home", f"{message!r} lost the fast lane"

    def test_a_time_without_an_action_is_not_a_job(self, iface):
        """"kolika je bila temperatura ujutro" is a sensor question."""
        assert not iface._looks_like_scheduled_request(
            "Kolika je bila temperatura ujutro"
        )

    def test_an_action_without_a_time_is_not_a_job(self, iface):
        assert not iface._looks_like_scheduled_request("Upali svjetlo u kuhinji")


class TestTheCroatianTheUserActuallySpeaks:
    @pytest.mark.parametrize(
        "phrase",
        ["za 10 minuta", "za 30 min", "za dva sata", "za 2 sata", "za sat vremena"],
    )
    def test_inflected_units_still_match(self, iface, phrase):
        """A word boundary after "minut" fails on "minuta" -- the form people say."""
        assert iface._looks_like_scheduled_request(f"{phrase} ugasi svjetlo"), phrase


class TestTheOrchestratorKnowsWhereToSendIt:
    """Routing there is useless if the prompt has never heard of the agent."""

    def test_the_scheduler_is_in_the_reference_table(self):
        from agents.adk_agents.adk_agent_factory import load_instruction_file

        prompt = load_instruction_file("orchestrator")
        assert "| scheduler |" in prompt

    def test_a_rule_says_later_means_the_scheduler(self):
        from agents.adk_agents.adk_agent_factory import load_instruction_file

        prompt = load_instruction_file("orchestrator")
        rule = prompt.split("### Rule 9d:")[1].split("### Rule 10")[0]
        assert "scheduler" in rule
        assert "Do not also perform the action." in rule
