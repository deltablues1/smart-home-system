"""An entity that remembers the conversation the Assist dialog throws away.

The state is the last thing asked, so Home Assistant's own history page shows a
timeline of questions. The answers live in the attributes, where a dashboard
card can show them -- which is the point: an answer that scrolled past, or that
was cut off when the app closed, is still there afterwards.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    HISTORY_ANSWER_CHARS,
    HISTORY_SHOWN_TURNS,
    SIGNAL_HISTORY_UPDATED,
)
from .history import JarvisHistory

# A state longer than 255 characters is rejected outright.
_STATE_CHARS = 250


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the conversation log entity."""
    history: JarvisHistory = hass.data[DOMAIN][entry.entry_id]["history"]
    async_add_entities([JarvisHistorySensor(entry, history)])


class JarvisHistorySensor(SensorEntity):
    """Last question as the state, recent turns as attributes."""

    _attr_name = "Jarvis razgovor"
    _attr_icon = "mdi:message-text-clock"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, history: JarvisHistory) -> None:
        self._attr_unique_id = f"{entry.entry_id}_razgovor"
        self._history = history

    async def async_added_to_hass(self) -> None:
        """Refresh whenever a turn is recorded."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_HISTORY_UPDATED, self._async_refresh
            )
        )

    @callback
    def _async_refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        last = self._history.last
        return _shorten(last["pitanje"], _STATE_CHARS) if last else None

    @property
    def extra_state_attributes(self) -> dict:
        turns = self._history.turns[:HISTORY_SHOWN_TURNS]
        last = turns[0] if turns else None
        return {
            "vrijeme": last["vrijeme"] if last else None,
            "pitanje": last["pitanje"] if last else None,
            "odgovor": last["odgovor"] if last else None,
            "povijest": [
                {
                    "vrijeme": turn["vrijeme"],
                    "pitanje": _shorten(turn["pitanje"], HISTORY_ANSWER_CHARS),
                    "odgovor": _shorten(turn["odgovor"], HISTORY_ANSWER_CHARS),
                }
                for turn in turns
            ],
        }
