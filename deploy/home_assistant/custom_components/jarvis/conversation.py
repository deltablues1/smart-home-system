"""Conversation agent that forwards Assist turns to the Jarvis web API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid as ulid_util

from .const import (
    CONF_SESSION_GRACE_MINUTES,
    CONF_TIMEOUT,
    CONF_TOKEN,
    CONF_URL,
    CONF_USER_ID,
    DEFAULT_SESSION_GRACE_MINUTES,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_ID,
    DOMAIN,
    SIGNAL_HISTORY_UPDATED,
)
from .history import JarvisHistory

_LOGGER = logging.getLogger(__name__)

# Markdown that reads as noise when a voice assistant speaks it aloud.
_MARKDOWN_CHARS = "*_`#"

# Everything from the arrows block upwards is symbols and emoji. Jarvis decorates
# answers with them for chat; spoken, they are either silence or gibberish.
# Croatian letters live far below this, so nothing real is lost.
_SYMBOL_START = 0x2190

# Conversation ids are never reused, so the map would otherwise grow for as long
# as Home Assistant stays up.
_MAX_TRACKED_CONVERSATIONS = 50


def _clean_for_speech(text: str) -> str:
    """Strip markdown and emoji, keep the line structure."""
    lines = []
    for raw_line in text.splitlines():
        kept = [
            char
            for char in raw_line
            if char not in _MARKDOWN_CHARS and ord(char) < _SYMBOL_START
        ]
        line = " ".join("".join(kept).split())
        if line:
            lines.append(line)
    return "\n".join(lines) or text.strip()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the Jarvis agent so it can be picked in an Assist pipeline."""
    async_add_entities([JarvisConversationEntity(hass, entry)])


class JarvisConversationEntity(conversation.ConversationEntity):
    """Passes what the user said to Jarvis and speaks back what it answered."""

    _attr_name = "Jarvis"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        # Home Assistant conversation_id -> Jarvis session_id. Without this each
        # turn would start a fresh Jarvis session and "and turn that one off too"
        # would have nothing to refer back to.
        self._sessions: dict[str, str] = {}
        # Reopening the Assist dialog produces a *new* conversation_id, so the
        # map above cannot carry a thread across it. These do.
        self._last_session: str | None = None
        self._last_used: datetime | None = None

    @property
    def supported_languages(self) -> list[str] | str:
        """Jarvis decides the language from the message itself."""
        return MATCH_ALL

    def _session_for(self, conversation_id: str, grace_minutes: float) -> str | None:
        """The session this turn belongs to, if there is a sensible one."""
        known = self._sessions.get(conversation_id)
        if known:
            return known
        if self._last_session and self._last_used and grace_minutes > 0:
            age = datetime.now(timezone.utc) - self._last_used
            if age <= timedelta(minutes=grace_minutes):
                _LOGGER.debug(
                    "Continuing the previous Jarvis session after %.0fs",
                    age.total_seconds(),
                )
                return self._last_session
        return None

    def _remember(self, conversation_id: str, session_id: str | None) -> None:
        """Note which session answered, and when, for the next turn."""
        if session_id:
            self._sessions[conversation_id] = session_id
            self._last_session = session_id
            while len(self._sessions) > _MAX_TRACKED_CONVERSATIONS:
                del self._sessions[next(iter(self._sessions))]
        self._last_used = datetime.now(timezone.utc)

    async def _record(self, question: str, answer: str) -> None:
        """Keep the turn where it can still be read once the dialog closes."""
        history: JarvisHistory = self.hass.data[DOMAIN][self.entry.entry_id]["history"]
        try:
            await history.async_add(question, answer)
        except Exception as err:  # noqa: BLE001 - never lose an answer over its copy
            _LOGGER.warning("Could not record the conversation turn: %s", err)
            return
        async_dispatcher_send(self.hass, SIGNAL_HISTORY_UPDATED)

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Send one turn to Jarvis and turn its reply into an Assist result."""
        config: dict[str, Any] = self.hass.data[DOMAIN][self.entry.entry_id]["config"]
        conversation_id = user_input.conversation_id or ulid_util.ulid_now()
        response = intent.IntentResponse(language=user_input.language)

        async def failed(message: str) -> conversation.ConversationResult:
            """Report a failure, and keep it in the log where it can be reread."""
            await self._record(user_input.text, message)
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE, message
            )
            return conversation.ConversationResult(
                response=response, conversation_id=conversation_id
            )

        payload: dict[str, Any] = {
            "message": user_input.text,
            "user_id": config.get(CONF_USER_ID, DEFAULT_USER_ID),
        }
        session_id = self._session_for(
            conversation_id,
            float(
                config.get(CONF_SESSION_GRACE_MINUTES, DEFAULT_SESSION_GRACE_MINUTES)
            ),
        )
        if session_id:
            payload["session_id"] = session_id

        url = config[CONF_URL].rstrip("/") + "/api/chat"
        headers = {"Authorization": "Bearer " + config[CONF_TOKEN]}
        timeout = aiohttp.ClientTimeout(
            total=float(config.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))
        )
        session = async_get_clientsession(self.hass)

        try:
            async with session.post(
                url, json=payload, headers=headers, timeout=timeout
            ) as api_response:
                if api_response.status in (401, 403):
                    _LOGGER.error("Jarvis rejected the API token")
                    return await failed("Jarvis je odbio token.")
                api_response.raise_for_status()
                data = await api_response.json()

        except TimeoutError:
            # Giving up on the reply does not cancel the work: Jarvis keeps
            # running and the task usually completes. Saying "it failed" here
            # was wrong -- on 2026-08-23 the document was written and the mail
            # was sent while the user was reading that it had timed out.
            _LOGGER.warning(
                "No reply within the timeout; Jarvis is probably still working"
            )
            return await failed(
                "Jarvis još radi na tome i ne stigne odgovoriti ovdje. "
                "Zadatak se vjerojatno ipak dovrši -- provjeri za koju minutu."
            )

        except (aiohttp.ClientError, ValueError) as err:
            _LOGGER.error("Could not reach Jarvis: %s", err)
            return await failed("Ne mogu doći do Jarvisa.")

        # Remember the session Jarvis used, so the next turn continues it.
        self._remember(conversation_id, data.get("session_id"))

        reply = (data.get("response") or "").strip()
        if not reply:
            _LOGGER.warning("Jarvis returned an empty reply for: %s", user_input.text)
            reply = "Jarvis nije vratio odgovor."

        spoken = _clean_for_speech(reply)
        await self._record(user_input.text, spoken)
        response.async_set_speech(spoken)
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )
