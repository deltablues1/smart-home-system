"""
Home Assistant tools must not hold the event loop, and must not lie about it.

ADK runs a synchronous tool inline, so every `urlopen` and `time.sleep` in the
TV and sensor tools blocked the whole process. `_volume_step_to` was the worst:
30 steps, each with two 10s HTTP timeouts, is ten minutes of frozen web, SSE
and voice.

The fix is two halves that only work together, and both are asserted here:

* the worker thread carries an absolute deadline, because `asyncio.to_thread`
  cannot be cancelled — a timeout on the awaiting side does not stop the work;
* the device lock is a `threading.Lock` held for the life of that thread, so a
  caller who gives up cannot let the next command reach the TV while the
  abandoned one is still sending keys.

And a timeout answers honestly: nothing sent is `aborted`, something sent is
`unknown`.

No network, no Home Assistant.

Run with:
    pytest tests/unit/test_ha_offload.py -v
"""

import asyncio
import inspect
import time

import pytest

from tools.adk_tools import _offload


# --- tools used as fixtures -------------------------------------------------

def _blocking_ok(seconds: float = 0.3) -> str:
    """Docstring kept on purpose: ADK uses it as the tool description."""
    time.sleep(seconds)
    return "ok"


def _runs_out_before_sending() -> str:
    """Burns its budget without ever reaching Home Assistant."""
    while True:
        _offload.check_deadline("test")
        time.sleep(0.02)


def _runs_out_after_sending() -> str:
    """Sends something, then loses the answer."""
    _offload.note_dispatch()
    while True:
        _offload.check_deadline("test")
        time.sleep(0.02)


# --- the loop stays free ----------------------------------------------------

class TestTheLoopKeepsRunning:

    @pytest.mark.asyncio
    async def test_a_blocking_tool_does_not_delay_other_work(self):
        tool = _offload.offload(default_deadline=5.0)(_blocking_ok)

        ticks = []

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks.append(time.monotonic())

        started = time.monotonic()
        result, _ = await asyncio.gather(tool(0.3), ticker())

        assert result == "ok"
        # Before the offload this ticker could not run at all until the
        # blocking call returned.
        assert ticks[-1] - started < 0.25
        assert time.monotonic() - started >= 0.3

    @pytest.mark.asyncio
    async def test_the_result_is_passed_through_untouched(self):
        def reads_a_sensor() -> dict:
            """Returns whatever HA said."""
            return {"success": True, "temperature": 21.5}

        tool = _offload.offload(default_deadline=5.0)(reads_a_sensor)
        assert await tool() == {"success": True, "temperature": 21.5}


# --- deadlines are enforced inside the thread -------------------------------

class TestTheDeadlineIsReal:

    @pytest.mark.asyncio
    async def test_nothing_sent_is_reported_as_aborted(self):
        tool = _offload.offload(default_deadline=0.15)(_runs_out_before_sending)

        result = await tool()

        assert result["outcome"] == "aborted"
        assert "ništa nije izvršeno" in result["error"]

    @pytest.mark.asyncio
    async def test_something_sent_is_reported_as_unknown(self):
        tool = _offload.offload(default_deadline=0.15)(_runs_out_after_sending)

        result = await tool()

        # The difference that matters: "failed" would invite a retry of a
        # command the TV may already have executed.
        assert result["outcome"] == "unknown"
        assert "ne znam" in result["error"]

    @pytest.mark.asyncio
    async def test_http_timeout_is_clamped_to_the_time_left(self):
        seen = {}

        def checks_its_budget() -> str:
            """Asks for a 10s HTTP timeout it is not entitled to."""
            seen["clamped"] = _offload.clamp_timeout(10.0)
            return "ok"

        tool = _offload.offload(default_deadline=2.0)(checks_its_budget)
        await tool()

        assert 0 < seen["clamped"] <= 2.0

    @pytest.mark.asyncio
    async def test_too_little_time_left_stops_before_sending(self):
        # There used to be a 0.5s floor here, so a call with 0.05s left was
        # still handed a 0.5s timeout — a hole in exactly the direction that
        # matters, since the call then outlived the budget it was respecting.
        def asks_twice() -> float:
            _offload.sleep(0.25)
            return _offload.clamp_timeout(10.0)

        tool = _offload.offload(default_deadline=0.3)(asks_twice)
        result = await tool()

        assert result["outcome"] == "aborted"
        assert "nije izvršeno" in result["error"]

    @pytest.mark.asyncio
    async def test_sleep_never_runs_past_the_deadline(self):
        def sleeps_too_long() -> float:
            """Wants 5s of warm-up out of a 0.3s budget."""
            return _offload.sleep(5.0)

        tool = _offload.offload(default_deadline=0.3)(sleeps_too_long)

        started = time.monotonic()
        slept = await tool()

        assert slept <= 0.35
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_without_a_deadline_nothing_is_clamped(self):
        # The same helpers are called from late_answer and the sensor module
        # outside any offloaded operation; they must stay inert there.
        assert _offload.remaining_seconds() is None
        assert _offload.clamp_timeout(10.0) == 10.0
        _offload.check_deadline("nije pod rokom")


# --- one device, one queue --------------------------------------------------

class TestDeviceSerialization:

    @pytest.mark.asyncio
    async def test_two_commands_to_one_device_do_not_overlap(self):
        windows = []

        def command(tag: str) -> str:
            """Records when it held the device."""
            start = time.monotonic()
            time.sleep(0.2)
            windows.append((tag, start, time.monotonic()))
            return tag

        tool = _offload.offload(device="tv-test-a", default_deadline=5.0)(command)

        await asyncio.gather(tool("prvi"), tool("drugi"))

        windows.sort(key=lambda w: w[1])
        (_, _, first_end), (_, second_start, _) = windows
        assert second_start >= first_end

    @pytest.mark.asyncio
    async def test_the_device_stays_locked_after_the_caller_gives_up(self):
        windows = []

        def command(tag: str, seconds: float) -> str:
            """The abandoned one keeps sending keys; the next must wait."""
            start = time.monotonic()
            time.sleep(seconds)
            windows.append((tag, start, time.monotonic()))
            return tag

        tool = _offload.offload(device="tv-test-b", default_deadline=5.0)(command)

        abandoned = asyncio.ensure_future(tool("napusten", 0.4))
        await asyncio.sleep(0.05)
        # The caller stops waiting. The thread cannot be cancelled, so the
        # only safe thing is for the device to stay busy.
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned

        await tool("sljedeci", 0.05)

        by_tag = {tag: (start, end) for tag, start, end in windows}
        assert "napusten" in by_tag, "the abandoned thread never finished"
        assert by_tag["sljedeci"][0] >= by_tag["napusten"][1]

    @pytest.mark.asyncio
    async def test_different_devices_run_in_parallel(self):
        def command() -> float:
            """One device must not queue behind another."""
            time.sleep(0.2)
            return time.monotonic()

        one = _offload.offload(device="tv-test-c", default_deadline=5.0)(command)
        two = _offload.offload(device="lampa-test", default_deadline=5.0)(command)

        started = time.monotonic()
        await asyncio.gather(one(), two())

        assert time.monotonic() - started < 0.35


# --- the model must see the same tools --------------------------------------

class TestTheToolSchemaSurvives:

    def test_wrapper_keeps_name_doc_and_signature(self):
        wrapped = _offload.offload(default_deadline=5.0)(_blocking_ok)

        assert inspect.iscoroutinefunction(wrapped)
        assert wrapped.__name__ == "_blocking_ok"
        assert wrapped.__doc__ == _blocking_ok.__doc__
        # inspect.signature follows __wrapped__, which is what ADK reads.
        assert inspect.signature(wrapped) == inspect.signature(_blocking_ok)

    def test_adk_builds_the_same_declaration(self):
        from google.adk.tools import FunctionTool

        def tv_volume_probe(action: str, level: int = 0) -> dict:
            """Upravljaj glasnoćom televizora."""
            return {"success": True}

        plain = FunctionTool(func=tv_volume_probe)._get_declaration()
        wrapped = FunctionTool(
            func=_offload.offload(device="tv", default_deadline=5.0)(tv_volume_probe)
        )._get_declaration()

        assert wrapped.name == plain.name
        assert wrapped.description == plain.description
        assert set(wrapped.parameters.properties) == set(plain.parameters.properties)

    def test_registered_ha_tools_are_all_async(self):
        from tools.adk_tools.ha_adk_tools import get_ha_adk_tools, tv_volume
        from tools.adk_tools.ha_sensor_tools import get_ha_sensor_tools

        tools = get_ha_adk_tools() + get_ha_sensor_tools()

        assert len(tools) == 17
        assert all(inspect.iscoroutinefunction(t) for t in tools)

        by_name = {t.__name__: t for t in tools}
        assert "ha_call_service" not in by_name  # still not exposed
        assert inspect.signature(by_name["tv_volume"]) == inspect.signature(tv_volume)


# --- a real lost answer, through the registered tool -------------------------

class TestARealTimeoutIsReportedAsUnknown:
    """The deadline tests raise the exception themselves. These do not.

    A genuine socket timeout is caught by `_ha_request`, which used to turn it
    into a plain `RuntimeError` — so `tv_turn_off()` answered
    `{"error": "HA API nedostupan (timed out)"}` for a command the television
    may well have carried out.
    """

    @pytest.fixture
    def ha_configured(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://ha.test:8123")
        monkeypatch.setenv("HA_TOKEN", "token")

    @staticmethod
    def _tool(name):
        from tools.adk_tools.ha_adk_tools import get_ha_adk_tools
        return {t.__name__: t for t in get_ha_adk_tools()}[name]

    @pytest.mark.asyncio
    async def test_a_command_whose_answer_is_lost_is_unknown(
        self, monkeypatch, ha_configured
    ):
        import tools.adk_tools.ha_adk_tools as ha

        def times_out(request, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(ha.urllib.request, "urlopen", times_out)

        result = await self._tool("tv_turn_off")()

        assert result["outcome"] == "unknown"
        assert "ne znam je li izvršena" in result["error"]

    @pytest.mark.asyncio
    async def test_a_command_that_never_left_is_a_plain_error(
        self, monkeypatch, ha_configured
    ):
        import tools.adk_tools.ha_adk_tools as ha

        def refused(request, timeout=None):
            raise ha.urllib.error.URLError(ConnectionRefusedError(61, "refused"))

        monkeypatch.setattr(ha.urllib.request, "urlopen", refused)

        result = await self._tool("tv_turn_off")()

        # Nothing was delivered, so this really is a failure and the agent may
        # safely try again.
        assert "error" in result
        assert result.get("outcome") != "unknown"

    @pytest.mark.asyncio
    async def test_a_lost_read_is_not_dressed_up_as_unknown(
        self, monkeypatch, ha_configured
    ):
        import tools.adk_tools.ha_adk_tools as ha

        def times_out(request, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(ha.urllib.request, "urlopen", times_out)

        result = await self._tool("tv_status")()

        # A GET changes nothing; losing its answer is an ordinary failure.
        assert result.get("outcome") != "unknown"


class TestOnlyARefusedConnectionProvesNothingHappened:
    """The question is asked in the conservative direction on purpose.

    Listing the failures that mean "unknown" leaves every unlisted one counted
    as proof of failure — which is how a plain ConnectionResetError came back
    as "HA API nedostupan" for a command the television had already carried
    out. Only a refused connection or an unresolvable host prove the request
    never left this machine.
    """

    @pytest.fixture
    def ha_configured(self, monkeypatch):
        monkeypatch.setenv("HA_URL", "http://ha.test:8123")
        monkeypatch.setenv("HA_TOKEN", "token")

    @staticmethod
    def _tool(name):
        from tools.adk_tools.ha_adk_tools import get_ha_adk_tools
        return {t.__name__: t for t in get_ha_adk_tools()}[name]

    async def _turn_off_with(self, monkeypatch, error):
        import tools.adk_tools.ha_adk_tools as ha

        def raises(request, timeout=None):
            raise error

        monkeypatch.setattr(ha.urllib.request, "urlopen", raises)
        return await self._tool("tv_turn_off")()

    @pytest.mark.asyncio
    async def test_a_connection_reset_is_unknown(self, monkeypatch, ha_configured):
        result = await self._turn_off_with(
            monkeypatch, ConnectionResetError(104, "connection reset by peer")
        )
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_broken_pipe_is_unknown(self, monkeypatch, ha_configured):
        result = await self._turn_off_with(
            monkeypatch, BrokenPipeError(32, "broken pipe")
        )
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_an_unresolvable_host_is_a_plain_error(
        self, monkeypatch, ha_configured
    ):
        import socket

        import tools.adk_tools.ha_adk_tools as ha

        result = await self._turn_off_with(
            monkeypatch, ha.urllib.error.URLError(socket.gaierror(-2, "no such host"))
        )
        assert result.get("outcome") != "unknown"

    @pytest.mark.asyncio
    async def test_a_server_error_mid_command_is_unknown(
        self, monkeypatch, ha_configured
    ):
        import io as _io

        import tools.adk_tools.ha_adk_tools as ha

        # Home Assistant got as far as trying. A service call can fail after
        # it has already reached the device, so a 5xx is not proof of nothing.
        error = ha.urllib.error.HTTPError(
            "http://ha.test:8123", 500, "Internal Server Error", {},
            _io.BytesIO(b"boom"),
        )
        result = await self._turn_off_with(monkeypatch, error)
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_rejection_is_a_plain_error(self, monkeypatch, ha_configured):
        import io as _io

        import tools.adk_tools.ha_adk_tools as ha

        # 4xx: understood, refused, nothing done.
        error = ha.urllib.error.HTTPError(
            "http://ha.test:8123", 404, "Not Found", {}, _io.BytesIO(b"nope")
        )
        result = await self._turn_off_with(monkeypatch, error)
        assert result.get("outcome") != "unknown"
