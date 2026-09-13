"""Regression tests for the 2026-08-18 "healthy zombie" incident.

The Telegram service logged a fatal startup error, never exited, and systemd
reported ``active (running)`` for two days. Three defects made that possible and
each one is pinned down here:

1. cleanup in ``finally:`` raised and masked the real error,
2. the fatal path used ``sys.exit()``, which non-daemon threads can veto,
3. nothing noticed that polling had stopped inside a live process.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.process_guard import (  # noqa: E402
    flush_logging,
    notify_watchdog,
    sd_notify,
    watchdog_interval_seconds,
)


# --- 1. shutdown must never mask the original failure ----------------------

def _interface_with_app(app):
    from interfaces.telegram_interface import TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.application = app
    return iface


def test_stop_survives_updater_that_never_started():
    """The exact incident: stop() on a never-started updater must not raise."""
    updater = MagicMock()
    updater.running = False
    updater.stop = AsyncMock(side_effect=RuntimeError("This Updater is not running!"))
    app = MagicMock()
    app.running = False
    app.updater = updater
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()

    asyncio.run(_interface_with_app(app).stop())

    updater.stop.assert_not_called()   # skipped: it was never running
    app.stop.assert_not_called()
    app.shutdown.assert_awaited()      # but the pool is still torn down


def test_stop_ignores_a_raising_shutdown_step():
    updater = MagicMock()
    updater.running = True
    updater.stop = AsyncMock(side_effect=RuntimeError("boom"))
    app = MagicMock()
    app.running = True
    app.updater = updater
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()

    asyncio.run(_interface_with_app(app).stop())  # must not raise

    app.shutdown.assert_awaited()


def test_stop_bounds_a_hanging_shutdown_step():
    """A wedged updater must not hold the process open either."""
    from interfaces.telegram_interface import TelegramInterface

    async def never_returns():
        await asyncio.sleep(3600)

    async def scenario():
        updater = MagicMock()
        updater.running = True
        updater.stop = MagicMock(return_value=never_returns())
        app = MagicMock()
        app.running = False
        app.updater = updater
        app.shutdown = AsyncMock()

        await asyncio.wait_for(_interface_with_app(app).stop(), timeout=5)
        app.shutdown.assert_awaited()

    original = TelegramInterface._safe_teardown

    async def quick(step, coro, timeout=0.05):
        await original(step, coro, timeout)

    with patch.object(TelegramInterface, "_safe_teardown", staticmethod(quick)):
        asyncio.run(scenario())


def test_stop_without_application_is_a_noop():
    asyncio.run(_interface_with_app(None).stop())


# --- 2. a dead poller inside a live process must be detected ---------------

def _supervised(app, **env):
    from interfaces.telegram_interface import PollingUnhealthy, TelegramInterface

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.application = app
    defaults = {
        "TELEGRAM_HEALTHCHECK_INTERVAL_SECONDS": "0.01",
        "TELEGRAM_HEALTHCHECK_PING_SECONDS": "0",
        "TELEGRAM_HEALTHCHECK_MAX_FAILURES": "2",
    }
    defaults.update(env)

    async def scenario():
        return await asyncio.wait_for(iface._supervise_polling(), timeout=5)

    with patch.dict(os.environ, defaults, clear=False):
        with pytest.raises(PollingUnhealthy) as excinfo:
            asyncio.run(scenario())
    return str(excinfo.value)


def test_supervisor_detects_stopped_updater():
    updater = MagicMock()
    updater.running = False
    app = MagicMock()
    app.running = True
    app.updater = updater

    assert "updater stopped polling" in _supervised(app)


def test_supervisor_detects_stopped_application():
    app = MagicMock()
    app.running = False

    assert "application stopped running" in _supervised(app)


def test_supervisor_stays_quiet_while_healthy():
    from interfaces.telegram_interface import TelegramInterface

    updater = MagicMock()
    updater.running = True
    app = MagicMock()
    app.running = True
    app.updater = updater

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.application = app

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(iface._supervise_polling(), timeout=0.3)

    with patch.dict(os.environ, {
        "TELEGRAM_HEALTHCHECK_INTERVAL_SECONDS": "0.01",
        "TELEGRAM_HEALTHCHECK_PING_SECONDS": "0",
    }, clear=False):
        asyncio.run(scenario())


def test_supervisor_tolerates_failed_pings_up_to_the_limit():
    updater = MagicMock()
    updater.running = True
    app = MagicMock()
    app.running = True
    app.updater = updater
    app.bot.get_me = AsyncMock(side_effect=OSError("network unreachable"))

    message = _supervised(app, **{
        "TELEGRAM_HEALTHCHECK_PING_SECONDS": "0.01",
        "TELEGRAM_HEALTHCHECK_MAX_FAILURES": "3",
    })

    assert "getMe failed 3 times" in message
    assert app.bot.get_me.await_count == 3


def test_healthcheck_can_be_disabled():
    from interfaces.telegram_interface import TelegramInterface

    app = MagicMock()
    app.running = False  # would normally trip the supervisor immediately

    iface = TelegramInterface.__new__(TelegramInterface)
    iface.application = app

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(iface._supervise_polling(), timeout=0.2)

    with patch.dict(os.environ, {
        "TELEGRAM_HEALTHCHECK_ENABLED": "false",
        "TELEGRAM_HEALTHCHECK_INTERVAL_SECONDS": "0.01",
        "TELEGRAM_HEALTHCHECK_PING_SECONDS": "0",
    }, clear=False):
        asyncio.run(scenario())


# --- 3. process_guard primitives ------------------------------------------

def test_flush_logging_does_not_hang_on_a_stuck_handler():
    class StuckHandler(logging.Handler):
        def flush(self):
            import time
            time.sleep(30)

        def emit(self, record):
            pass

    root = logging.getLogger()
    handler = StuckHandler()
    root.addHandler(handler)
    try:
        assert flush_logging(timeout=0.2) is False  # gave up instead of hanging
    finally:
        root.removeHandler(handler)


def test_sd_notify_is_a_noop_without_systemd():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOTIFY_SOCKET", None)
        assert sd_notify("WATCHDOG=1") is False
        assert notify_watchdog() is False


def test_watchdog_interval_is_half_of_watchdog_usec():
    with patch.dict(os.environ, {"WATCHDOG_USEC": "180000000"}, clear=False):
        assert watchdog_interval_seconds() == 90.0
    with patch.dict(os.environ, {"WATCHDOG_USEC": "nonsense"}, clear=False):
        assert watchdog_interval_seconds() is None
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("WATCHDOG_USEC", None)
        assert watchdog_interval_seconds() is None


@pytest.mark.skipif(not hasattr(__import__("socket"), "AF_UNIX"), reason="needs AF_UNIX")
def test_sd_notify_sends_to_the_socket(tmp_path):
    import socket

    path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    try:
        with patch.dict(os.environ, {"NOTIFY_SOCKET": path}, clear=False):
            assert sd_notify("WATCHDOG=1") is True
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()
