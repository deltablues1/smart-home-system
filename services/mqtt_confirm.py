"""Publish-and-confirm layer for smart-home MQTT commands.

"status: ok" used to mean only "publish was called" — the device may have been
offline and nobody would know. ESP32 devices echo their real state on
``esp32-io/.../state`` topics, so confirmation is: subscribe first, publish the
command, then wait until every expected state is observed (or a timeout).

Pragmatic constraints (paho-mqtt on an RPi, not a message bus cluster):
- one short-lived client per operation, no background daemon
- the confirm layer must NEVER block the action: if anything in it fails,
  commands are blindly published and marked "unconfirmed"
- everything is synchronous inside a worker thread; the public API is async

Env:
    MQTT_CONFIRM_ENABLED  (default true; false restores fire-and-forget)
    MQTT_CONFIRM_TIMEOUT  (seconds to wait for state echoes, default 3.0)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class DeviceCommand:
    """One MQTT command plus how to recognize its confirmation."""

    name: str                    # result key, e.g. "svjetlo_kuhinja"
    command_topic: str
    payload: str
    state_topic: str
    # Either the exact expected state payload ("ON") or a predicate over the
    # observed payload (for JSON dimmer states).
    expected: Union[str, Callable[[str], bool]]

    def matches(self, observed_payload: str) -> bool:
        if callable(self.expected):
            try:
                return bool(self.expected(observed_payload))
            except Exception:
                return False
        return observed_payload.strip() == self.expected


def expect_json_state(
    state: str,
    brightness: Optional[int] = None,
    tolerance: int = 10,
) -> Callable[[str], bool]:
    """Predicate for JSON state payloads like {"state": "ON", "brightness": 64}.

    When ``brightness`` is given (and state is ON), the observed brightness
    must match within ``tolerance`` — otherwise a lamp already ON at 100%
    would falsely confirm a request for 25%.
    """

    def _match(payload: str) -> bool:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, AttributeError):
            return False
        if data.get("state") != state:
            return False
        if brightness is not None and state == "ON":
            observed = data.get("brightness")
            if observed is None:
                return False
            try:
                return abs(int(observed) - int(brightness)) <= tolerance
            except (TypeError, ValueError):
                return False
        return True

    return _match


def _confirm_enabled() -> bool:
    return os.getenv("MQTT_CONFIRM_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _confirm_timeout() -> float:
    try:
        return float(os.getenv("MQTT_CONFIRM_TIMEOUT", "3.0"))
    except ValueError:
        return 3.0


def _default_client_factory():
    from tools.adk_tools.mqtt_adk_tools import _get_mqtt_client

    return _get_mqtt_client()


# Injectable for tests (FakeMqttClient) — module-level on purpose.
_client_factory = _default_client_factory


def _blind_publish(commands: List[DeviceCommand]) -> None:
    """Fire-and-forget fallback: the action must not be lost because the
    confirmation machinery failed. Waits for broker delivery (qos=1) where
    the client supports it, so "sent" at least means "broker accepted"."""
    client = _client_factory()
    try:
        for cmd in commands:
            result = client.publish(cmd.command_topic, cmd.payload, qos=1)
            wait = getattr(result, "wait_for_publish", None)
            if callable(wait):
                try:
                    wait(timeout=5)
                except Exception:
                    pass
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# How long to let the broker replay retained states before publishing —
# retained messages must land in the baseline, not count as echoes.
_RETAINED_DRAIN_SECONDS = 0.25


def _confirm_worker(commands: List[DeviceCommand], timeout: float) -> Dict[str, dict]:
    """Sync worker: subscribe → drain retained states → publish → await echoes.

    Retained/baseline separation: the broker replays each topic's retained
    (possibly stale) state right after subscribe. If a device is OFFLINE but
    the broker retains exactly the requested state, counting that replay as
    confirmation would be a false positive. So states received BEFORE the
    publish go into a baseline; per-device outcome is:
      - "confirmed"        — a state matching the expectation arrived AFTER publish
      - "already_in_state" — no echo, but the baseline already matched (device
                             was presumably in the target state; weaker signal)
      - "timeout"          — neither
    """
    confirmed: Dict[str, bool] = {cmd.name: False for cmd in commands}
    baseline_match: Dict[str, bool] = {cmd.name: False for cmd in commands}
    observed: Dict[str, Optional[str]] = {cmd.name: None for cmd in commands}
    all_confirmed = threading.Event()
    lock = threading.Lock()
    published = threading.Event()

    def on_message(_client, _userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace")
        # Broker marks retained replays explicitly — they are baseline no
        # matter WHEN they arrive (a late retained packet must never count
        # as a fresh echo).
        is_retained = bool(getattr(msg, "retain", False))
        with lock:
            for cmd in commands:
                if cmd.state_topic == msg.topic:
                    observed[cmd.name] = payload
                    is_baseline = is_retained or not published.is_set()
                    if cmd.matches(payload):
                        if is_baseline:
                            baseline_match[cmd.name] = True
                        else:
                            confirmed[cmd.name] = True
                    elif not is_baseline:
                        # Post-publish observation CONTRADICTS the target
                        # state — the device is observably NOT there, so a
                        # stale baseline match must not report success.
                        confirmed[cmd.name] = False
                        baseline_match[cmd.name] = False
            if all(confirmed.values()):
                all_confirmed.set()

    client = _client_factory()
    try:
        client.on_message = on_message
        # Subscribe BEFORE publishing so the echo cannot be missed.
        for topic in {cmd.state_topic for cmd in commands}:
            client.subscribe(topic, qos=1)
        client.loop_start()
        try:
            # Let retained replay land in the baseline (belt-and-braces next
            # to the msg.retain check — some brokers/bridges drop the flag).
            time.sleep(_RETAINED_DRAIN_SECONDS)
            published.set()
            for cmd in commands:
                client.publish(cmd.command_topic, cmd.payload, qos=1)
            all_confirmed.wait(timeout)
        finally:
            client.loop_stop()
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    with lock:
        results: Dict[str, dict] = {}
        for name in confirmed:
            if confirmed[name]:
                status = "confirmed"
            elif baseline_match[name]:
                status = "already_in_state"
            else:
                status = "timeout"
            results[name] = {"status": status, "observed": observed[name]}
        return results


async def publish_and_confirm(
    commands: List[DeviceCommand],
    timeout: Optional[float] = None,
) -> dict:
    """Publish commands and wait for device state confirmation.

    Returns::

        {
            "status": "confirmed" | "partial" | "timeout" | "sent" | "error",
            "operation_id": "1a2b3c4d",
            "confirmed": 3, "total": 5,
            "devices": {name: {"status": ..., "observed": ...}, ...},
        }

    "sent" = confirmation disabled (published fire-and-forget, NOT confirmed);
    "partial" = some but not all devices confirmed;
    "error" = even the blind publish failed (broker unreachable).
    Per-device "already_in_state" counts as success (baseline matched).

    LIMITATION: this observes state topics, not per-command ACKs. A true ACK
    would need the ESP32 firmware to echo the operation_id with each state
    change — until then, a concurrent automation flipping the same device can
    in principle be attributed to this command.
    """
    operation_id = uuid.uuid4().hex[:8]
    timeout = timeout if timeout is not None else _confirm_timeout()
    total = len(commands)
    loop = asyncio.get_event_loop()

    if not _confirm_enabled():
        try:
            await loop.run_in_executor(None, _blind_publish, commands)
            logger.info(
                f"[{operation_id}] MQTT published unconfirmed "
                "(confirm layer disabled)"
            )
            return {
                "status": "sent",
                "operation_id": operation_id,
                "confirmed": 0,
                "total": total,
                "devices": {
                    cmd.name: {"status": "unconfirmed", "observed": None}
                    for cmd in commands
                },
            }
        except Exception as e:
            logger.error(f"[{operation_id}] MQTT publish failed: {e}")
            return {
                "status": "error",
                "operation_id": operation_id,
                "error": str(e),
                "confirmed": 0,
                "total": total,
                "devices": {},
            }

    try:
        devices = await loop.run_in_executor(None, _confirm_worker, commands, timeout)
    except Exception as e:
        # Confirmation machinery failed — degrade to blind publish so the
        # user's action still happens, and be honest that it's unconfirmed.
        logger.warning(f"[{operation_id}] confirm worker failed ({e}); blind publish")
        try:
            await loop.run_in_executor(None, _blind_publish, commands)
            return {
                "status": "partial" if total > 1 else "unconfirmed",
                "operation_id": operation_id,
                "confirmed": 0,
                "total": total,
                "devices": {
                    cmd.name: {"status": "unconfirmed", "observed": None}
                    for cmd in commands
                },
                "warning": f"confirmation unavailable: {e}",
            }
        except Exception as publish_err:
            logger.error(f"[{operation_id}] MQTT publish failed: {publish_err}")
            return {
                "status": "error",
                "operation_id": operation_id,
                "error": str(publish_err),
                "confirmed": 0,
                "total": total,
                "devices": {},
            }

    # The per-device layer is careful to separate "the device echoed the new
    # state" from "the retained baseline already matched", and says in its own
    # docstring that the second is a weaker signal. Summing them and calling
    # the total "confirmed" threw that away — so when the ESP32 dropped off
    # MQTT on 2026-09-03 and left its state retained, every command came back
    # "confirmed" and Jarvis said "u redu" for three days while nothing moved.
    #
    # A device that only matched its baseline has told us nothing about this
    # command. The aggregate now says so.
    echoed = sum(1 for d in devices.values() if d["status"] == "confirmed")
    baseline_only = sum(1 for d in devices.values() if d["status"] == "already_in_state")
    confirmed_count = echoed + baseline_only

    if echoed == total:
        status = "confirmed"
    elif confirmed_count == total:
        # Nothing was refused, but nothing reported back either: every device
        # was already sitting in the requested state, as far as the broker
        # knows. True when the house is already as you want it, and also true
        # when the device is gone and its last state is stale.
        status = "already_in_state"
    elif confirmed_count > 0:
        status = "partial"
    else:
        status = "timeout"

    logger.info(
        f"[{operation_id}] MQTT confirm: {confirmed_count}/{total} devices ({status})"
    )
    return {
        "status": status,
        "operation_id": operation_id,
        "confirmed": confirmed_count,
        "total": total,
        "devices": devices,
    }
