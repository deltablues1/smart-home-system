"""
Home Assistant MQTT Discovery bridge for the Raspberry Pi voice interface.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _truncate_state_text(text: str, limit: int = 255) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


class HomeAssistantMqttBridge:
    def __init__(
        self,
        api_base_url: str,
        voice_assistant_name: str,
        mode_command_callback: Optional[Callable[[str], None]] = None,
        listening_command_callback: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.enabled = _env_flag("HA_MQTT_DISCOVERY_ENABLED", True)
        self.broker = os.getenv("MQTT_BROKER", "").strip()
        self.port = int(os.getenv("MQTT_PORT", "1883"))
        self.username = os.getenv("MQTT_USER", "").strip()
        self.password = os.getenv("MQTT_PASS", "").strip()
        # Retained transcripts persist spoken conversations in the broker (and
        # any broker backup) — privacy default is OFF; HA still receives live
        # updates, they just don't survive an HA restart.
        self.retain_transcripts = _env_flag("HA_BRIDGE_RETAIN_TRANSCRIPTS", False)
        self.discovery_prefix = os.getenv("HA_MQTT_DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"
        self.state_prefix = os.getenv("HA_MQTT_STATE_PREFIX", "google_clause/rpi_voice").strip() or "google_clause/rpi_voice"
        self.node_id = os.getenv("HA_MQTT_NODE_ID", "google_clause").strip() or "google_clause"
        self.device_id = os.getenv("HA_MQTT_DEVICE_ID", "google_clause_rpi_voice").strip() or "google_clause_rpi_voice"
        self.device_name = os.getenv("HA_MQTT_DEVICE_NAME", voice_assistant_name).strip() or voice_assistant_name
        self.manufacturer = os.getenv("HA_MQTT_DEVICE_MANUFACTURER", "Google Clause").strip() or "Google Clause"
        self.model = os.getenv("HA_MQTT_DEVICE_MODEL", "Raspberry Pi Voice Gateway").strip() or "Raspberry Pi Voice Gateway"
        self.api_base_url = api_base_url.rstrip("/")
        self.mode_command_callback = mode_command_callback
        self.listening_command_callback = listening_command_callback
        self.client = None
        self._last_states: dict[str, str] = {}

    @property
    def availability_topic(self) -> str:
        return f"{self.state_prefix}/availability"

    @property
    def voice_mode_state_topic(self) -> str:
        return f"{self.state_prefix}/voice_mode/state"

    @property
    def voice_mode_command_topic(self) -> str:
        return f"{self.state_prefix}/voice_mode/set"

    @property
    def listening_state_topic(self) -> str:
        return f"{self.state_prefix}/listening/state"

    @property
    def listening_command_topic(self) -> str:
        return f"{self.state_prefix}/listening/set"

    @property
    def last_transcript_topic(self) -> str:
        return f"{self.state_prefix}/last_transcript/state"

    @property
    def last_response_topic(self) -> str:
        return f"{self.state_prefix}/last_response/state"

    def is_available(self) -> bool:
        return self.enabled and bool(self.broker)

    def _device_payload(self) -> dict:
        return {
            "identifiers": [self.device_id],
            "name": self.device_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "configuration_url": self.api_base_url,
        }

    def build_discovery_messages(self) -> list[tuple[str, dict]]:
        device = self._device_payload()
        base = f"{self.discovery_prefix}"
        prefix = f"{base}"
        object_base = f"{self.node_id}/{self.device_id}"
        return [
            (
                f"{prefix}/binary_sensor/{object_base}_online/config",
                {
                    "name": "Online",
                    "unique_id": f"{self.device_id}_online",
                    "state_topic": self.availability_topic,
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device_class": "connectivity",
                    "entity_category": "diagnostic",
                    "device": device,
                },
            ),
            (
                f"{prefix}/select/{object_base}_voice_mode/config",
                {
                    "name": "Voice mode",
                    "unique_id": f"{self.device_id}_voice_mode",
                    "state_topic": self.voice_mode_state_topic,
                    "command_topic": self.voice_mode_command_topic,
                    "options": ["agent", "live"],
                    "icon": "mdi:microphone-message",
                    "availability_topic": self.availability_topic,
                    "device": device,
                },
            ),
            (
                f"{prefix}/switch/{object_base}_listening/config",
                {
                    "name": "Slušanje",
                    "unique_id": f"{self.device_id}_listening",
                    "state_topic": self.listening_state_topic,
                    "command_topic": self.listening_command_topic,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:microphone",
                    "availability_topic": self.availability_topic,
                    "device": device,
                },
            ),
            (
                f"{prefix}/sensor/{object_base}_last_transcript/config",
                {
                    "name": "Last transcript",
                    "unique_id": f"{self.device_id}_last_transcript",
                    "state_topic": self.last_transcript_topic,
                    "icon": "mdi:text-box-outline",
                    "availability_topic": self.availability_topic,
                    "device": device,
                },
            ),
            (
                f"{prefix}/sensor/{object_base}_last_response/config",
                {
                    "name": "Last response",
                    "unique_id": f"{self.device_id}_last_response",
                    "state_topic": self.last_response_topic,
                    "icon": "mdi:account-voice",
                    "availability_topic": self.availability_topic,
                    "device": device,
                },
            ),
        ]

    def start(self, initial_voice_mode: str, initial_listening: bool = False) -> None:
        if not self.is_available():
            if self.enabled and not self.broker:
                logger.info("HA MQTT discovery enabled, but MQTT_BROKER is not configured")
            return

        import paho.mqtt.client as mqtt
        import uuid

        # Unique suffix: two bridges with the same fixed client_id would keep
        # disconnecting each other at the broker.
        client_id = f"{self.device_id}-{uuid.uuid4().hex[:8]}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if self.username or self.password:
            self.client.username_pw_set(self.username, self.password)
        if _env_flag("MQTT_TLS", False):
            self.client.tls_set()
        self.client.will_set(self.availability_topic, payload="offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        try:
            # connect_async hands the retrying to paho's network thread. The
            # blocking connect() that stood here raised OSError when this Pi
            # booted before the Home Assistant machine answered, and that
            # exception travelled all the way up and left the wake-word service
            # running with no bridge at all.
            self.client.connect_async(self.broker, self.port, keepalive=30)
            self.client.loop_start()
        except Exception as err:  # noqa: BLE001 - the assistant matters more
            logger.warning("HA MQTT bridge could not start its client: %s", err)
            self.client = None
            return
        # Recorded now, put on the wire by _on_connect once the broker answers.
        self.update_online(True)
        self.update_voice_mode(initial_voice_mode)
        self.update_listening(initial_listening)

    def stop(self) -> None:
        if not self.client:
            return
        try:
            self.update_online(False)
            self.client.loop_stop()
            self.client.disconnect()
        finally:
            self.client = None

    def _republish_all(self) -> None:
        """Discovery plus every state we hold, as if the bridge had just started."""
        self.publish_discovery()
        transcript_topics = {self.last_transcript_topic, self.last_response_topic}
        for topic, state in list(self._last_states.items()):
            retain = self.retain_transcripts if topic in transcript_topics else True
            self._publish_state(topic, state, retain=retain)

    def publish_discovery(self) -> None:
        if not self.client:
            return
        for topic, payload in self.build_discovery_messages():
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)

    def update_online(self, online: bool) -> None:
        self._publish_state(self.availability_topic, "online" if online else "offline")

    def update_voice_mode(self, mode: str) -> None:
        self._publish_state(self.voice_mode_state_topic, mode)

    def update_listening(self, listening: bool) -> None:
        """Mirror the wake-word listening switch to HA.

        Retained on purpose: HA must know, right after its own restart, whether
        the microphone is live. The service always republishes the real state on
        startup, so a stale retained ON can never outlive the process.
        """
        self._publish_state(self.listening_state_topic, "ON" if listening else "OFF")

    def update_last_transcript(self, text: str) -> None:
        self._publish_state(
            self.last_transcript_topic, _truncate_state_text(text),
            retain=self.retain_transcripts,
        )

    def update_last_response(self, text: str) -> None:
        self._publish_state(
            self.last_response_topic, _truncate_state_text(text),
            retain=self.retain_transcripts,
        )

    def _publish_state(self, topic: str, payload: str, retain: bool = True) -> None:
        self._last_states[topic] = payload
        if not self.client:
            return
        self.client.publish(topic, payload, qos=1, retain=retain)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if getattr(reason_code, "value", reason_code) != 0:
            logger.warning("HA MQTT bridge connect failed: %s", reason_code)
            return
        client.subscribe(self.voice_mode_command_topic, qos=1)
        client.subscribe(self.listening_command_topic, qos=1)
        client.subscribe(f"{self.discovery_prefix}/status", qos=1)
        # Every connect is a first impression: after a broker restart, or a
        # boot where the broker was not up yet, nothing of ours is on the wire.
        self._republish_all()

    def _on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if msg.topic == f"{self.discovery_prefix}/status" and payload.lower() == "online":
            self._republish_all()
            return

        if msg.topic == self.listening_command_topic:
            requested = payload.upper()
            if requested not in {"ON", "OFF"}:
                logger.warning("Ignoring unsupported HA listening command: %s", payload)
                return
            if self.listening_command_callback:
                self.listening_command_callback(requested == "ON")
            return

        if msg.topic != self.voice_mode_command_topic:
            return

        requested_mode = payload.lower()
        if requested_mode not in {"agent", "live"}:
            logger.warning("Ignoring unsupported HA voice mode command: %s", payload)
            return
        if self.mode_command_callback:
            self.mode_command_callback(requested_mode)
