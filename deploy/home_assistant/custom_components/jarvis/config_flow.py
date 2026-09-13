"""Config flow for the Jarvis conversation agent."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MAX_TURN_SECONDS,
    CONF_SESSION_GRACE_MINUTES,
    CONF_SILENCE_SECONDS,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
    CONF_USER_ID,
    DEFAULT_MAX_TURN_SECONDS,
    DEFAULT_SESSION_GRACE_MINUTES,
    DEFAULT_SILENCE_SECONDS,
    DEFAULT_TIMEOUT,
    DEFAULT_URL,
    DEFAULT_USER_ID,
    DOMAIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): str,
        vol.Required(CONF_TOKEN): str,
        vol.Optional(CONF_USER_ID, default=DEFAULT_USER_ID): str,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): int,
    }
)


class InvalidAuth(Exception):
    """The API token was rejected."""


async def _async_validate(hass, data: dict[str, Any]) -> None:
    """Reach Jarvis once before saving, so a typo fails here and not at runtime."""
    session = async_get_clientsession(hass)
    url = data[CONF_URL].rstrip("/") + "/api/status"
    headers = {"Authorization": "Bearer " + data[CONF_TOKEN]}
    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
    ) as response:
        if response.status in (401, 403):
            raise InvalidAuth
        response.raise_for_status()


class JarvisConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the Jarvis URL and token."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _async_validate(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - any failure means "cannot reach it"
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Jarvis", data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Voice timings are worth tuning by ear, so keep them out of a file."""
        return JarvisOptionsFlow()


class JarvisOptionsFlow(config_entries.OptionsFlow):
    """Adjust how long Jarvis waits -- for a sentence, and for an answer."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SILENCE_SECONDS,
                    default=float(
                        current.get(CONF_SILENCE_SECONDS, DEFAULT_SILENCE_SECONDS)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.3, max=10)),
                vol.Optional(
                    CONF_MAX_TURN_SECONDS,
                    default=float(
                        current.get(CONF_MAX_TURN_SECONDS, DEFAULT_MAX_TURN_SECONDS)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=5, max=120)),
                vol.Optional(
                    CONF_TIMEOUT,
                    default=int(current.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=900)),
                vol.Optional(
                    CONF_SESSION_GRACE_MINUTES,
                    default=float(
                        current.get(
                            CONF_SESSION_GRACE_MINUTES, DEFAULT_SESSION_GRACE_MINUTES
                        )
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=120)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
