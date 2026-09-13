"""Jarvis as a Home Assistant conversation agent.

Home Assistant cannot point its Assist pipeline at an arbitrary HTTP service:
the built-in options are OpenAI, Anthropic and Google, and any of those would be
a different assistant without Jarvis's tools. This integration closes that gap
by forwarding conversation turns to the Jarvis web API and returning its reply.

Two things around it are handled here because Home Assistant offers no setting
for either: ``assist_patience`` widens the end-of-speech timing, and ``history``
keeps the turns that the Assist dialog discards when it closes.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .assist_patience import apply_patience, restore_patience
from .const import (
    CONF_MAX_TURN_SECONDS,
    CONF_SILENCE_SECONDS,
    DEFAULT_MAX_TURN_SECONDS,
    DEFAULT_SILENCE_SECONDS,
    DOMAIN,
)
from .history import JarvisHistory

PLATFORMS = ["conversation", "sensor"]

# Where the pre-Jarvis Assist timings are kept so unloading can put them back.
_PATIENCE = "assist_patience"


def _setting(entry: ConfigEntry, key: str, default: float) -> float:
    """Options win over the values given when the entry was created."""
    return float(entry.options.get(key, entry.data.get(key, default)))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jarvis from a config entry."""
    config = dict(entry.data)
    config.update(entry.options)

    history = JarvisHistory(hass)
    await history.async_load()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "config": config,
        "history": history,
    }
    hass.data[DOMAIN][_PATIENCE] = apply_patience(
        _setting(entry, CONF_SILENCE_SECONDS, DEFAULT_SILENCE_SECONDS),
        _setting(entry, CONF_MAX_TURN_SECONDS, DEFAULT_MAX_TURN_SECONDS),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and hand Assist's own timings back."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data.get(DOMAIN, {})
        data.pop(entry.entry_id, None)
        restore_patience(data.pop(_PATIENCE, {}))
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the user edits the options."""
    await hass.config_entries.async_reload(entry.entry_id)
