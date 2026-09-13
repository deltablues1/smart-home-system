"""Keeps what was said, because the Assist dialog does not.

Home Assistant's voice dialog holds a conversation only while it is open. Close
it -- or let the phone close it for you -- and both the answer and the thread
are gone, which is how a long reply can vanish before it has been read.

This keeps the recent turns in Home Assistant's own storage, so they can be read
from a dashboard afterwards and survive a restart. It is deliberately small: a
ring buffer of the last few turns, not a searchable archive. Jarvis keeps the
real conversation on its own side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import HISTORY_STORED_TURNS

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY = "jarvis_conversation_history"


class JarvisHistory:
    """The last few question-and-answer pairs, kept across restarts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._turns: list[dict] = []

    @property
    def turns(self) -> list[dict]:
        """Newest first."""
        return list(self._turns)

    @property
    def last(self) -> dict | None:
        return self._turns[0] if self._turns else None

    async def async_load(self) -> None:
        """Read what was kept from before. A broken file must not block setup."""
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read the saved conversation: %s", err)
            return
        if isinstance(stored, dict):
            turns = stored.get("turns")
            if isinstance(turns, list):
                self._turns = turns[:HISTORY_STORED_TURNS]

    async def async_add(self, question: str, answer: str) -> dict:
        """Record one turn and return it."""
        turn = {
            "vrijeme": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pitanje": question,
            "odgovor": answer,
        }
        self._turns.insert(0, turn)
        del self._turns[HISTORY_STORED_TURNS:]
        # delay=... would batch writes, but an answer worth keeping is exactly
        # the one the phone is about to interrupt.
        await self._store.async_save({"turns": self._turns})
        return turn

    async def async_clear(self) -> None:
        self._turns = []
        await self._store.async_save({"turns": []})
