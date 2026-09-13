"""Tests for the conversation log that outlives the Assist dialog.

Home Assistant is not installed here, so the two pieces the module touches --
the type annotation and the storage helper -- are stood in for. The storage stub
keeps what it was given, which is exactly what the tests need to assert.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class _FakeStore:
    """Stands in for homeassistant.helpers.storage.Store."""

    # Set by a test to make loading fail the way a corrupt file would.
    load_error: Exception | None = None
    # What a fresh instance should find on disk.
    preloaded: object = None

    def __init__(self, hass, version, key):
        self.key = key
        self.saved = _FakeStore.preloaded

    async def async_load(self):
        if _FakeStore.load_error is not None:
            raise _FakeStore.load_error
        return self.saved

    async def async_save(self, data):
        self.saved = data


def _install_homeassistant_stubs() -> None:
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _FakeStore
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.storage = storage
    root = types.ModuleType("homeassistant")
    root.core = core
    root.helpers = helpers
    sys.modules.setdefault("homeassistant", root)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.storage", storage)


_install_homeassistant_stubs()

_COMPONENT_DIR = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "home_assistant"
    / "custom_components"
    / "jarvis"
)

# history.py imports ".const", so it has to be loaded as part of a package. The
# component's own __init__.py pulls in half of Home Assistant, so a bare package
# pointed at the same directory stands in for it.
_package = types.ModuleType("jarvis_component")
_package.__path__ = [str(_COMPONENT_DIR)]
sys.modules["jarvis_component"] = _package

_spec = importlib.util.spec_from_file_location(
    "jarvis_component.history", _COMPONENT_DIR / "history.py"
)
history_module = importlib.util.module_from_spec(_spec)
sys.modules["jarvis_component.history"] = history_module
_spec.loader.exec_module(history_module)

JarvisHistory = history_module.JarvisHistory
STORED = history_module.HISTORY_STORED_TURNS


@pytest.fixture(autouse=True)
def _clean_store():
    _FakeStore.load_error = None
    _FakeStore.preloaded = None
    yield
    _FakeStore.load_error = None
    _FakeStore.preloaded = None


def _history() -> "JarvisHistory":
    return JarvisHistory(hass=object())


def test_a_turn_is_kept_with_both_sides_and_a_time():
    history = _history()

    turn = asyncio.run(history.async_add("upali svjetlo", "Upalio sam svjetlo."))

    assert turn["pitanje"] == "upali svjetlo"
    assert turn["odgovor"] == "Upalio sam svjetlo."
    assert turn["vrijeme"]
    assert history.last == turn


def test_the_newest_turn_comes_first():
    history = _history()

    async def scenario():
        await history.async_add("prvo", "A")
        await history.async_add("drugo", "B")

    asyncio.run(scenario())

    assert [t["pitanje"] for t in history.turns] == ["drugo", "prvo"]


def test_old_turns_fall_off_the_end():
    """Unbounded growth would eventually be written to disk on every turn."""
    history = _history()

    async def scenario():
        for i in range(STORED + 5):
            await history.async_add(f"pitanje {i}", f"odgovor {i}")

    asyncio.run(scenario())

    assert len(history.turns) == STORED
    assert history.last["pitanje"] == f"pitanje {STORED + 4}"


def test_every_turn_is_written_out_immediately():
    """The answer worth keeping is the one the phone is about to interrupt."""
    history = _history()

    asyncio.run(history.async_add("pitanje", "odgovor"))

    assert history._store.saved["turns"][0]["pitanje"] == "pitanje"


def test_what_was_saved_before_is_read_back():
    _FakeStore.preloaded = {
        "turns": [{"vrijeme": "2026-08-23T18:00:00+00:00", "pitanje": "p", "odgovor": "o"}]
    }
    history = _history()

    asyncio.run(history.async_load())

    assert history.last["pitanje"] == "p"


def test_a_stored_file_longer_than_the_limit_is_trimmed_on_load():
    _FakeStore.preloaded = {
        "turns": [{"vrijeme": "t", "pitanje": str(i), "odgovor": "o"} for i in range(STORED + 10)]
    }
    history = _history()

    asyncio.run(history.async_load())

    assert len(history.turns) == STORED


def test_an_unreadable_file_does_not_block_setup():
    """A broken log is a lost log, not a broken integration."""
    _FakeStore.load_error = ValueError("not json")
    history = _history()

    asyncio.run(history.async_load())

    assert history.turns == []


def test_nonsense_in_the_file_is_ignored():
    _FakeStore.preloaded = {"turns": "ovo nije popis"}
    history = _history()

    asyncio.run(history.async_load())

    assert history.turns == []


def test_nothing_saved_yet_is_not_an_error():
    history = _history()

    asyncio.run(history.async_load())

    assert history.turns == []
    assert history.last is None


def test_clearing_empties_both_memory_and_disk():
    history = _history()

    async def scenario():
        await history.async_add("pitanje", "odgovor")
        await history.async_clear()

    asyncio.run(scenario())

    assert history.turns == []
    assert history._store.saved == {"turns": []}
