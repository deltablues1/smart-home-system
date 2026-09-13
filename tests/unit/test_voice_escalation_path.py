"""voice_qa's escalation must actually reach the orchestrator.

voice_qa has no tools. When a spoken request turns out to need one, it answers
with [[ESCALATE]] and the interface re-runs the message through the full
orchestrator. That hand-off called _run_orchestration_for_voice without `ctx`,
which is a required parameter -- so every escalation raised TypeError before
the orchestrator ran, and the user heard "tehnicka greska" for a request the
system was perfectly able to carry out.

Seen 2026-09-06 on "Dodaj mi na kupovnu listu kruh, ulje i mlijeko": the phrase
matched no smart-home keyword, fell to voice_qa, escalated, and died on the
missing argument. The sibling call site fifty lines further down had always
passed ctx, which is why this went unnoticed.

Signature drift is the whole risk here, so the test binds the real signature
rather than asserting on a mock's kwargs.

Run with:
    pytest tests/unit/test_voice_escalation_path.py -v
"""

import inspect
from types import SimpleNamespace

import pytest

from interfaces.base_interface import ESCALATE_SENTINEL, BaseInterface


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


class TestTheHandOffBinds:
    """Both call sites must satisfy the real signature."""

    def test_ctx_is_required(self):
        sig = inspect.signature(BaseInterface._run_orchestration_for_voice)
        ctx = sig.parameters["ctx"]
        assert ctx.default is inspect.Parameter.empty, (
            "if ctx ever gains a default, this whole class of bug goes quiet "
            "again -- decide deliberately before changing it"
        )

    @pytest.mark.asyncio
    async def test_the_escalation_calls_it_with_every_required_argument(
        self, iface, monkeypatch
    ):
        captured = {}
        sig = inspect.signature(BaseInterface._run_orchestration_for_voice)

        async def spy(self, *args, **kwargs):
            # Binding against the real signature is the point: a missing
            # required argument raises here exactly as it did in production.
            bound = sig.bind(self, *args, **kwargs)
            captured.update(bound.arguments)
            return "orchestrator answer"

        async def fake_worker(agent, message, **kwargs):
            return ESCALATE_SENTINEL

        monkeypatch.setattr(
            BaseInterface, "_run_orchestration_for_voice", spy, raising=True
        )
        monkeypatch.setattr(iface, "_get_worker_agent", lambda name: object())
        # Imported inside the method, so the patch has to land on the source
        # module -- patching the importing one would silently do nothing.
        monkeypatch.setattr(
            "agents.adk_agents.runner_utils.run_agent_simple", fake_worker
        )

        # Only what this path touches: ctx.helper is read for the ADK
        # session service before the worker runs.
        ctx = SimpleNamespace(helper=SimpleNamespace(session_service=None))
        answer = await iface._run_direct_worker_agent(
            agent_name="voice_qa",
            user_id="u",
            session_id="s",
            message="Dodaj mi na kupovnu listu kruh, ulje i mlijeko.",
            ctx=ctx,
        )

        assert answer == "orchestrator answer"
        assert captured["ctx"] is ctx, "the turn's context must be carried over"
        assert captured["question"] == "Dodaj mi na kupovnu listu kruh, ulje i mlijeko."


class TestThePhrasingThatExposedIt:
    def test_kupovna_lista_reaches_the_house_agent(self, iface):
        for message in (
            "Dodaj mi na kupovnu listu kruh, ulje i mlijeko.",
            "Sto je na kupovnoj listi?",
        ):
            _, target = iface._classify_voice_route(message)
            assert target == "smart_home", message
