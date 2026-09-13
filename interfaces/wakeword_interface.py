"""
Raspberry Pi wake-word voice interface.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from config.deployment_config import get_deployment_config, is_wake_word_enabled
from config.voice_persona import get_voice_assistant_name
from services.ha_mqtt_bridge import HomeAssistantMqttBridge
from services.audio_ingress import get_audio_ingress_service

from .base_interface import BaseInterface

logger = logging.getLogger(__name__)


def looks_like_noise_transcript(text: str) -> bool:
    """Heuristic for background/appliance noise the STT hallucinated into text.

    Three patterns cover what we see in practice: strings of isolated digits
    ("3 7 1 2 0 6 8 3 8 3" from a washing machine), transcripts with no
    letters at all ("...", "123"), and STT non-speech placeholders like
    "[nečujno]" / "(nerazumljivo)". Real speech with numbers ("koliko je 2 i
    2", "21:23") has letter tokens and is not flagged.
    """
    text = (text or "").strip()
    if not any(ch.isalpha() for ch in text):
        return True

    # STT engines emit bracketed placeholders for non-speech audio; those must
    # never reach the agent as a real query.
    lowered = text.lower()
    placeholders = (
        "nečujno", "necujno", "nerazumljivo", "inaudible", "unintelligible",
        "glazba", "music", "tišina", "tisina", "silence", "šum", "sum]",
    )
    if (
        (lowered.startswith("[") or lowered.startswith("("))
        and (lowered.endswith("]") or lowered.endswith(")"))
        and any(p in lowered for p in placeholders)
    ):
        return True

    tokens = text.split()
    digit_tokens = sum(1 for token in tokens if token.isdigit())
    return digit_tokens >= 4 and digit_tokens >= 0.6 * len(tokens)


class WakeWordInterface(BaseInterface):
    def __init__(self):
        super().__init__(session_prefix="wakeword")
        if not is_wake_word_enabled():
            raise ValueError("Wake word interface is disabled by deployment profile")

        deployment = get_deployment_config()
        self.voice_mode = deployment.voice_mode_default
        self.api_base_url = os.getenv("WAKEWORD_API_BASE_URL", "http://127.0.0.1:8000")
        self.api_token = os.getenv("API_TOKEN", "")
        self.user_id = os.getenv("WAKEWORD_USER_ID", "wakeword-user")
        self.session_id = self.generate_session_id(self.user_id)
        self.ha_mqtt_bridge: Optional[HomeAssistantMqttBridge] = None

        # The microphone is opt-in. An always-listening mic on a shared room
        # device turned music playing on a nearby speaker into wake events that
        # ran a full agent turn each -- unnoticed for two days, because the
        # wm8960 amplifier had gone to sleep and nobody heard the answers.
        # Default OFF means the expensive failure mode needs a deliberate act.
        self.listening_enabled = os.getenv(
            "WAKEWORD_LISTEN_ON_START", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            auto_off_minutes = float(os.getenv("WAKEWORD_AUTO_OFF_MINUTES", "30"))
        except ValueError:
            auto_off_minutes = 30.0
        # 0 disables the timer -- for the case where someone genuinely wants an
        # always-on room mic and is choosing that with their eyes open.
        self.listening_auto_off_seconds = max(0.0, auto_off_minutes) * 60.0
        self._listening_deadline: Optional[float] = None

    def format_response(self, response: str) -> str:
        return response

    async def start(self) -> None:
        self.initialize_system()
        self.ha_mqtt_bridge = HomeAssistantMqttBridge(
            api_base_url=self.api_base_url,
            voice_assistant_name=get_voice_assistant_name(),
            mode_command_callback=self._handle_external_voice_mode_command,
            listening_command_callback=self._handle_external_listening_command,
        )
        self.ha_mqtt_bridge.start(
            initial_voice_mode=self.voice_mode,
            initial_listening=self.listening_enabled,
        )
        if self.listening_enabled:
            self._arm_auto_off()
        logger.info(
            "WakeWordInterface initialized in %s mode (listening=%s, auto-off=%.0fmin)",
            self.voice_mode,
            "on" if self.listening_enabled else "off",
            self.listening_auto_off_seconds / 60.0,
        )

    async def stop(self) -> None:
        if self.ha_mqtt_bridge:
            self.ha_mqtt_bridge.stop()
        logger.info("WakeWordInterface stopped")

    def get_live_ws_url(self) -> str:
        base = self.api_base_url.rstrip("/")
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        elif base.startswith("wss://") or base.startswith("ws://"):
            ws_base = base
        else:
            ws_base = f"ws://{base}"

        if self.api_token:
            return f"{ws_base}/api/live?token={self.api_token}"
        return f"{ws_base}/api/live"

    def _extract_mode_switch(self, transcript: str) -> Optional[str]:
        text = transcript.lower().strip()
        if any(phrase in text for phrase in (
            "vrati na agent mod",
            "prebaci na agent mod",
            "izadi iz live moda",
            "prekini live mod",
            "zatvori live mod",
            "vrati se na agent mod",
            "agent mod",
        )):
            return "agent"
        if any(phrase in text for phrase in (
            "prebaci na live mod",
            "ukljuci live mod",
            "idi u live mod",
            "live mod",
        )):
            return "live"
        return None

    def detect_mode_switch(self, transcript: str) -> Optional[str]:
        return self._extract_mode_switch(transcript)

    def set_voice_mode(self, mode: str) -> dict:
        if mode not in {"agent", "live"}:
            raise ValueError(f"Unsupported voice mode: {mode}")
        self.voice_mode = mode
        if self.ha_mqtt_bridge:
            self.ha_mqtt_bridge.update_voice_mode(mode)
        return {
            "mode": self.voice_mode,
            "response": f"Prebacen sam u {self.voice_mode} mod.",
        }

    def set_listening(self, listening: bool) -> dict:
        """Turn the wake-word microphone on or off.

        The wake loop watches this flag and releases the audio device entirely
        while it is False, so "off" means the mic is not open -- not merely
        that detections are discarded.
        """
        changed = listening != self.listening_enabled
        self.listening_enabled = listening
        self._arm_auto_off() if listening else self._disarm_auto_off()
        if self.ha_mqtt_bridge:
            self.ha_mqtt_bridge.update_listening(listening)
        if changed:
            logger.info("Wake-word listening %s", "enabled" if listening else "disabled")
        return {
            "listening": self.listening_enabled,
            "response": "Slušam." if listening else "Mikrofon je ugašen.",
        }

    def _arm_auto_off(self) -> None:
        self._listening_deadline = (
            time.monotonic() + self.listening_auto_off_seconds
            if self.listening_auto_off_seconds > 0
            else None
        )

    def _disarm_auto_off(self) -> None:
        self._listening_deadline = None

    def note_listening_activity(self) -> None:
        """Push the auto-off deadline back; called on every wake event."""
        if self.listening_enabled:
            self._arm_auto_off()

    def check_listening_auto_off(self) -> bool:
        """Turn listening off after a quiet spell. Returns True if it fired.

        The safety net for the real failure: a switch flipped on and forgotten.
        Lives here rather than in a Home Assistant automation so it still works
        when HA is down -- which is exactly when nobody is watching.
        """
        if not self.listening_enabled or self._listening_deadline is None:
            return False
        if time.monotonic() < self._listening_deadline:
            return False
        logger.info(
            "Wake-word listening auto-off after %.0f min idle",
            self.listening_auto_off_seconds / 60.0,
        )
        self.set_listening(False)
        return True

    def _handle_external_listening_command(self, listening: bool) -> None:
        try:
            self.set_listening(listening)
            logger.info("Listening changed from HA MQTT command: %s", listening)
        except Exception:
            logger.exception("Failed to apply HA MQTT listening command: %s", listening)

    def _handle_external_voice_mode_command(self, mode: str) -> None:
        try:
            self.set_voice_mode(mode)
            logger.info("Voice mode changed from HA MQTT command: %s", mode)
        except Exception:
            logger.exception("Failed to apply HA MQTT voice mode command: %s", mode)

    async def process_transcript(self, transcript: str) -> dict:
        if self.ha_mqtt_bridge:
            self.ha_mqtt_bridge.update_last_transcript(transcript)

        requested_mode = self.detect_mode_switch(transcript)
        if requested_mode:
            self.set_voice_mode(requested_mode)
            result = {
                "mode": self.voice_mode,
                "transcript": transcript,
                "response": f"Prebacen sam u {self.voice_mode} mod.",
            }
            if self.ha_mqtt_bridge:
                self.ha_mqtt_bridge.update_last_response(result["response"])
            return result

        if self.voice_mode == "live":
            result = {
                "mode": self.voice_mode,
                "transcript": transcript,
                "response": (
                    "Live mod za Pi koristi postojeci /api/live websocket i raw audio stream. "
                    "Taj hardware streaming nije aktiviran u ovom simulacijskom runneru."
                ),
            }
            if self.ha_mqtt_bridge:
                self.ha_mqtt_bridge.update_last_response(result["response"])
            return result

        # RAW transcript into the pipeline: routing, the smart-home fast path
        # and the noise gate must see the user's words only. The voice persona
        # is injected at the LLM boundary inside BaseInterface (wrapping here
        # once made the intent gate block every single wakeword message).
        response = await self.process_message(
            user_id=self.user_id,
            message=transcript,
            session_id=self.session_id,
        )
        result = {
            "mode": self.voice_mode,
            "transcript": transcript,
            "response": response,
        }
        if self.ha_mqtt_bridge:
            self.ha_mqtt_bridge.update_last_response(response)
        return result

    async def process_audio(self, audio_bytes: bytes, mime_type: str, source: str = "wakeword") -> dict:
        transcript_result = await get_audio_ingress_service().transcribe_audio(
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            source=source,
            metadata={"session_id": self.session_id},
        )
        logger.info("STT transcript: %r", transcript_result.transcript)

        # Noise gate: STT hallucinates digit strings out of appliance noise.
        # Reject BEFORE the agent call — no LLM cost, no spoken answer to a
        # washing machine ("Zbroj tih brojeva je 41").
        if looks_like_noise_transcript(transcript_result.transcript):
            logger.info(
                "Noise transcript rejected before agent call: %r",
                transcript_result.transcript[:80],
            )
            return {
                "mode": self.voice_mode,
                "transcript": transcript_result.transcript,
                "response": "",
                "noise": True,
                "metadata": transcript_result.metadata,
            }

        result = await self.process_transcript(transcript_result.transcript)
        result["metadata"] = transcript_result.metadata
        return result
