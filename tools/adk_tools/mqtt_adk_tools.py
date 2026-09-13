"""
MQTT ADK Tools - Smart Home upravljanje preko ESP32 + Home Assistant

Svi alati komuniciraju s ESP32-IO MQTT brokerom za upravljanje
svjetlima, utičnicama i dimmerom u kući.
"""

import json
import logging
import asyncio
import os
from typing import Optional

logger = logging.getLogger(__name__)

# MQTT broker config
MQTT_BROKER = os.getenv("MQTT_BROKER", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "").strip()
MQTT_PASS = os.getenv("MQTT_PASS", "").strip()

# Topic prefixes
SWITCH_PREFIX = "esp32-io/switch"
LIGHT_PREFIX = "esp32-io/light"
SENSOR_PREFIX = "esp32-io/binary_sensor"

# Valid switch names (lights + outlets)
VALID_LIGHTS = [
    "svjetlo_vani", "svjetlo_terasa1", "svjetlo_terasa2", "svjetlo_ulaz",
    "svjetlo_hidrofor", "svjetlo_tv", "svjetlo_stup", "svjetlo_boravak",
    "svjetlo_blagavaona", "svjetlo_kuhinja", "svjetlo_sank", "svjetlo_hodnik",
    "svjetlo_kupaona", "svjetlo_soba1", "svjetlo_soba2"
]

VALID_OUTLETS = [
    "uticnica_ulaz", "uticnica_kuhinja", "uticnica_frizider", "uticnica_kupaona",
    "uticnica_bojler", "uticnica_terasa", "uticnica_tv", "uticnica_boravak",
    "uticnica_blagavaona", "pecnica", "uticnica_soba1", "uticnica_soba2",
    "slobodno1", "slobodno2"
]

VALID_SWITCHES = VALID_LIGHTS + VALID_OUTLETS

# Code-level guards (the agent prompt alone is not enforcement):
# - fridge/boiler OFF spoils food / kills hot water -> require explicit confirm
# - oven ON while nobody is watching is a fire risk -> require explicit confirm
#
# Env-driven so plugging something that must not lose power into a new outlet
# is a config change, not a commit and a redeploy. Defaults are the devices
# that were hardcoded here, so an unset variable protects what it always did.
def _protected_from_env(var: str, default: set) -> set:
    raw = os.getenv(var)
    if raw is None:
        return set(default)
    return {name.strip() for name in raw.split(",") if name.strip()}


PROTECTED_OFF_DEVICES = _protected_from_env(
    "SMART_HOME_PROTECTED_OFF", {"uticnica_frizider", "uticnica_bojler"}
)
PROTECTED_ON_DEVICES = _protected_from_env("SMART_HOME_PROTECTED_ON", {"pecnica"})

# --- Turn-gated approvals for protected actions -----------------------------
# The mechanism lives in services/approvals.py now; these are the names the
# rest of the code already imports, kept as a thin shim. confirm=True alone
# still proves nothing — a protected action becomes redeemable only after the
# user confirms in their NEXT message, which the model cannot fabricate.
from services import approvals as _approvals

_PROTECTED_LANE = "smart_home"


def _action_id(device_name: str, state: str) -> str:
    return _approvals.fingerprint("mqtt_switch_control", device=device_name, state=state)


def set_approval_session(session_id: str) -> None:
    """Bind subsequent register/redeem calls to this session's context."""
    _approvals.set_session(session_id)


def register_pending_approval(device_name: str, state: str) -> None:
    friendly = DEVICE_NAMES.get(device_name, device_name)
    _approvals.register(
        _action_id(device_name, state),
        question=f"{friendly} -> {state}",
        lane=_PROTECTED_LANE,
    )


def arm_pending_approvals(session_id: str) -> None:
    """The user of *session_id* explicitly confirmed."""
    _approvals.on_user_turn(session_id, affirmative=True)


def cancel_pending_approvals(session_id: str) -> None:
    """The user said no / changed topic."""
    _approvals.cancel(session_id)


def has_pending_approval(session_id: str) -> bool:
    return _approvals.has_pending(session_id)


def redeem_approval(device_name: str, state: str) -> bool:
    """Consume an armed, unexpired approval for the current session."""
    return _approvals.redeem(_action_id(device_name, state))



# Human-readable names (Croatian)
DEVICE_NAMES = {
    "svjetlo_vani": "Vanjsko svjetlo",
    "svjetlo_terasa1": "Terasa 1",
    "svjetlo_terasa2": "Terasa 2",
    "svjetlo_ulaz": "Ulaz",
    "svjetlo_hidrofor": "Hidrofor",
    "svjetlo_tv": "TV svjetlo",
    "svjetlo_stup": "Stup",
    "svjetlo_boravak": "Boravak",
    "svjetlo_blagavaona": "Blagavaona",
    "svjetlo_kuhinja": "Kuhinja",
    "svjetlo_sank": "Šank",
    "svjetlo_hodnik": "Hodnik",
    "svjetlo_kupaona": "Kupaona",
    "svjetlo_soba1": "Soba 1",
    "svjetlo_soba2": "Soba 2",
    "svjetlo_fotelja": "Fotelja (dimmer)",
    "uticnica_ulaz": "Utičnica ulaz",
    "uticnica_kuhinja": "Utičnica kuhinja",
    "uticnica_frizider": "Frižider",
    "uticnica_kupaona": "Utičnica kupaona",
    "uticnica_bojler": "Bojler",
    "uticnica_terasa": "Utičnica terasa",
    "uticnica_tv": "Utičnica TV",
    "uticnica_boravak": "Utičnica boravak",
    "uticnica_blagavaona": "Utičnica blagavaona",
    "pecnica": "Pećnica",
    "uticnica_soba1": "Utičnica soba 1",
    "uticnica_soba2": "Utičnica soba 2",
    "slobodno1": "Rezerva 1",
    "slobodno2": "Rezerva 2",
}


def _get_mqtt_client():
    """Create and connect MQTT client (sync)"""
    import paho.mqtt.client as mqtt

    if not MQTT_BROKER:
        raise RuntimeError("MQTT_BROKER is not configured")

    client = mqtt.Client()
    if MQTT_USER or MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    if os.getenv("MQTT_TLS", "").strip().lower() in ("1", "true", "yes", "on"):
        client.tls_set()
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=10)
    return client


def _publish_and_disconnect(topic: str, payload: str) -> dict:
    """Publish a single message and disconnect"""
    try:
        client = _get_mqtt_client()
        result = client.publish(topic, payload, qos=1)
        result.wait_for_publish(timeout=5)
        client.disconnect()
        return {"status": "ok", "topic": topic, "payload": payload}
    except Exception as e:
        logger.error(f"MQTT publish failed: {e}")
        return {"status": "error", "error": str(e)}


# ============================================================================
# ADK TOOL FUNCTIONS
# ============================================================================

async def mqtt_switch_control(device_name: str, state: str, confirm: bool = False) -> dict:
    """
    Turn a light or outlet ON or OFF.

    Controls any switch-type device in the smart home system (lights and outlets).
    Note: turning on svjetlo_kupaona automatically turns off bojler (hardware interlock).

    PROTECTED actions (uticnica_frizider/uticnica_bojler OFF, pecnica ON)
    need confirm=True — and the confirmation only works after the USER has
    replied in a NEW message since the needs_confirmation response (turn-gated
    approval; same-turn confirm=True is rejected). Flow: call normally →
    needs_confirmation → ask the user → user replies "da" → call again with
    confirm=True.

    NOTE (hardware limitation): turning svjetlo_kupaona ON switches the
    boiler OFF via a hardware interlock — no software guard can intercept it.

    Args:
        device_name: Device identifier, e.g. "svjetlo_kuhinja", "uticnica_tv", "pecnica".
                     Use mqtt_list_devices to see all available devices.
        state: "ON" or "OFF"
        confirm: True only after the user explicitly confirmed a protected action.

    Returns:
        Dictionary with status, topic, and payload sent.
    """
    state = state.upper().strip()
    if state not in ("ON", "OFF"):
        return {"status": "error", "error": f"State must be ON or OFF, got: {state}"}

    device_name = device_name.strip().lower()
    if device_name not in VALID_SWITCHES:
        return {
            "status": "error",
            "error": f"Unknown device: {device_name}. Use mqtt_list_devices to see valid names.",
        }

    protected = (
        (state == "OFF" and device_name in PROTECTED_OFF_DEVICES)
        or (state == "ON" and device_name in PROTECTED_ON_DEVICES)
    )
    if protected:
        friendly = DEVICE_NAMES.get(device_name, device_name)
        if not confirm or not redeem_approval(device_name, state):
            # Either no confirm, or confirm=True without a user turn in
            # between — register/refresh and demand a real user confirmation.
            register_pending_approval(device_name, state)
            return {
                "status": "needs_confirmation",
                "device": friendly,
                "requested_state": state,
                "message": (
                    f"'{friendly} -> {state}' je zaštićena radnja. Potvrda mora "
                    "doći od korisnika u SLJEDEĆOJ poruci: pitaj korisnika, "
                    "pričekaj njegov odgovor, pa ponovi poziv s confirm=True."
                ),
            }

    topic = f"{SWITCH_PREFIX}/{device_name}/command"
    friendly = DEVICE_NAMES.get(device_name, device_name)

    from services.mqtt_confirm import DeviceCommand, publish_and_confirm

    outcome = await publish_and_confirm([
        DeviceCommand(
            name=device_name,
            command_topic=topic,
            payload=state,
            state_topic=f"{SWITCH_PREFIX}/{device_name}/state",
            expected=state,
        )
    ])
    device_result = outcome.get("devices", {}).get(device_name, {})
    result = {
        "status": outcome["status"],
        "operation_id": outcome.get("operation_id"),
        "topic": topic,
        "payload": state,
        "observed_state": device_result.get("observed"),
        "device": friendly,
        "action": f"{'Uključeno' if state == 'ON' else 'Isključeno'}: {friendly}",
    }
    if outcome.get("error"):
        result["error"] = outcome["error"]
    logger.info(f"MQTT switch: {friendly} -> {state} ({outcome['status']})")
    return result


async def mqtt_dimmer_control(state: str, brightness: int = 255) -> dict:
    """
    Control the dimmable armchair light (svjetlo_fotelja).

    Args:
        state: "ON" or "OFF"
        brightness: Brightness level 0-255 (64=25%, 128=50%, 191=75%, 255=100%).
                    Only used when state is "ON".

    Returns:
        Dictionary with status and payload sent.
    """
    state = state.upper().strip()
    if state not in ("ON", "OFF"):
        return {"status": "error", "error": f"State must be ON or OFF, got: {state}"}

    brightness = max(0, min(255, int(brightness)))

    if state == "ON":
        payload = json.dumps({"state": "ON", "brightness": brightness})
    else:
        payload = json.dumps({"state": "OFF"})

    topic = f"{LIGHT_PREFIX}/svjetlo_fotelja/command"

    from services.mqtt_confirm import DeviceCommand, expect_json_state, publish_and_confirm

    outcome = await publish_and_confirm([
        DeviceCommand(
            name="svjetlo_fotelja",
            command_topic=topic,
            payload=payload,
            state_topic=f"{LIGHT_PREFIX}/svjetlo_fotelja/state",
            expected=expect_json_state(
                state, brightness=brightness if state == "ON" else None
            ),
        )
    ])
    device_result = outcome.get("devices", {}).get("svjetlo_fotelja", {})
    result = {
        "status": outcome["status"],
        "operation_id": outcome.get("operation_id"),
        "topic": topic,
        "payload": payload,
        "observed_state": device_result.get("observed"),
        "device": "Fotelja (dimmer)",
    }
    if outcome.get("error"):
        result["error"] = outcome["error"]
    if state == "ON":
        pct = round(brightness / 255 * 100)
        result["action"] = f"Fotelja upaljena na {pct}%"
    else:
        result["action"] = "Fotelja ugašena"
    logger.info(f"MQTT dimmer: fotelja -> {state} (brightness={brightness}, {outcome['status']})")
    return result


async def mqtt_scene_control(scene: str) -> dict:
    """
    Activate a predefined scene (multiple devices at once).

    Available scenes:
    - "sve_ugasi" : Turn off ALL lights and outlets (except frizider and bojler)
    - "nocno" : Night mode - only hodnik at low brightness + fotelja at 25%
    - "film" : Movie mode - TV light + TV outlet ON, fotelja 25%, all other
               lights and outlets OFF (except frizider and bojler)
    - "dolazak" : Arrival - ulaz + hodnik + boravak + vani ON
    - "odlazak" : Leaving - everything OFF except frizider and bojler
    - "kuhanje" : Cooking - kuhinja + sank + blagavaona ON

    Args:
        scene: Scene name from the list above.

    Returns:
        Dictionary with status and list of actions performed.
    """
    scene = scene.strip().lower()

    # Mass-off never touches protected devices (fridge, boiler); scenes must
    # request those explicitly via mqtt_switch_control(confirm=True).
    safe_outlets_off = [o for o in VALID_OUTLETS if o not in PROTECTED_OFF_DEVICES]

    scenes = {
        "sve_ugasi": {
            "switches_off": VALID_LIGHTS + safe_outlets_off,
            "dimmer": {"state": "OFF"},
            "description": "Sve ugašeno",
        },
        "nocno": {
            "switches_off": [l for l in VALID_LIGHTS if l not in ("svjetlo_hodnik",)],
            "switches_on": ["svjetlo_hodnik"],
            "dimmer": {"state": "ON", "brightness": 64},
            "description": "Noćni režim",
        },
        "film": {
            # Prompt promises "ostalo OFF": lights AND outlets except the TV
            # corner and protected devices.
            "switches_off": (
                [l for l in VALID_LIGHTS if l not in ("svjetlo_tv",)]
                + [o for o in safe_outlets_off if o != "uticnica_tv"]
            ),
            "switches_on": ["svjetlo_tv", "uticnica_tv"],
            "dimmer": {"state": "ON", "brightness": 64},
            "description": "Filmski režim",
        },
        "dolazak": {
            "switches_on": ["svjetlo_ulaz", "svjetlo_hodnik", "svjetlo_boravak", "svjetlo_vani"],
            "description": "Dolazak kući",
        },
        "odlazak": {
            "switches_off": VALID_LIGHTS + safe_outlets_off,
            "dimmer": {"state": "OFF"},
            "description": "Odlazak - sve ugašeno (osim frižidera i bojlera)",
        },
        "kuhanje": {
            "switches_on": ["svjetlo_kuhinja", "svjetlo_sank", "svjetlo_blagavaona"],
            "description": "Kuhanje",
        },
    }

    if scene not in scenes:
        return {
            "status": "error",
            "error": f"Unknown scene: {scene}. Available: {', '.join(scenes.keys())}",
        }

    cfg = scenes[scene]

    from services.mqtt_confirm import DeviceCommand, expect_json_state, publish_and_confirm

    commands = []
    actions = []

    for dev in cfg.get("switches_off", []):
        commands.append(DeviceCommand(
            name=dev,
            command_topic=f"{SWITCH_PREFIX}/{dev}/command",
            payload="OFF",
            state_topic=f"{SWITCH_PREFIX}/{dev}/state",
            expected="OFF",
        ))
        actions.append(f"{DEVICE_NAMES.get(dev, dev)} -> OFF")

    for dev in cfg.get("switches_on", []):
        commands.append(DeviceCommand(
            name=dev,
            command_topic=f"{SWITCH_PREFIX}/{dev}/command",
            payload="ON",
            state_topic=f"{SWITCH_PREFIX}/{dev}/state",
            expected="ON",
        ))
        actions.append(f"{DEVICE_NAMES.get(dev, dev)} -> ON")

    if "dimmer" in cfg:
        d = cfg["dimmer"]
        commands.append(DeviceCommand(
            name="svjetlo_fotelja",
            command_topic=f"{LIGHT_PREFIX}/svjetlo_fotelja/command",
            payload=json.dumps(d),
            state_topic=f"{LIGHT_PREFIX}/svjetlo_fotelja/state",
            expected=expect_json_state(d["state"], brightness=d.get("brightness")),
        ))
        if d["state"] == "ON":
            pct = round(d.get("brightness", 255) / 255 * 100)
            actions.append(f"Fotelja -> ON ({pct}%)")
        else:
            actions.append("Fotelja -> OFF")

    outcome = await publish_and_confirm(commands)
    if outcome["status"] == "error":
        logger.error(f"MQTT scene '{scene}' failed: {outcome.get('error')}")
        return {"status": "error", "error": outcome.get("error"), "scene": scene}

    unconfirmed = [
        DEVICE_NAMES.get(name, name)
        for name, dev_result in outcome.get("devices", {}).items()
        if dev_result["status"] != "confirmed"
    ]
    confirmed_count = outcome.get("confirmed", 0)
    total = outcome.get("total", len(commands))

    logger.info(
        f"MQTT scene '{scene}': {confirmed_count}/{total} confirmed "
        f"({outcome['status']})"
    )
    return {
        "status": outcome["status"],
        "operation_id": outcome.get("operation_id"),
        "scene": scene,
        "description": cfg["description"],
        "actions": actions,
        "total_actions": len(actions),
        "confirmed": confirmed_count,
        "total": total,
        "unconfirmed_devices": unconfirmed,
        "summary": f"{confirmed_count}/{total} potvrđeno",
    }


async def mqtt_get_status() -> dict:
    """
    Read current state of all devices by subscribing to state topics.

    Connects to MQTT broker, subscribes to all state topics, waits for responses,
    and returns the current state of all lights, outlets, dimmer, and sensors.

    Returns:
        Dictionary with device states grouped by category (lights, outlets, dimmer, sensors).
    """
    import paho.mqtt.client as mqtt
    import time

    states = {}
    received_event = asyncio.Event()

    def on_message(client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        states[topic] = payload

    def _subscribe_and_collect():
        client = mqtt.Client()
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_message = on_message
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=10)

        # Subscribe to all state topics
        client.subscribe(f"{SWITCH_PREFIX}/+/state")
        client.subscribe(f"{LIGHT_PREFIX}/+/state")
        client.subscribe(f"{SENSOR_PREFIX}/+/state")
        client.subscribe("esp32-io/status")

        # Collect messages for 2 seconds
        client.loop_start()
        time.sleep(2)
        client.loop_stop()
        client.disconnect()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _subscribe_and_collect)

    # Organize results
    lights = {}
    outlets = {}
    dimmer = {}
    sensors = {}
    system = {}

    for topic, value in states.items():
        # 2-segment controller status topic; must be handled before the
        # len(parts) >= 3 gate or it is silently dropped.
        if topic == "esp32-io/status":
            system["esp32_status"] = value
            continue

        parts = topic.split("/")
        if len(parts) >= 3:
            device = parts[2] if len(parts) > 2 else parts[-1]
            friendly = DEVICE_NAMES.get(device, device)

            if "light/" in topic:
                try:
                    data = json.loads(value)
                    dimmer[friendly] = data
                except json.JSONDecodeError:
                    dimmer[friendly] = value
            elif "binary_sensor/" in topic:
                sensors[friendly] = value
            elif "switch/" in topic:
                if device in VALID_LIGHTS:
                    lights[friendly] = value
                elif device in VALID_OUTLETS:
                    outlets[friendly] = value
                else:
                    system[device] = value

    return {
        "status": "ok",
        "lights": lights,
        "outlets": outlets,
        "dimmer": dimmer,
        "sensors": sensors,
        "system": system,
        "total_devices": len(lights) + len(outlets) + len(dimmer),
    }


async def mqtt_list_devices() -> dict:
    """
    List all available smart home devices with their MQTT topics.

    Returns a complete list of controllable devices grouped by type
    (lights, dimmer, outlets) with device IDs and friendly names.

    Returns:
        Dictionary with all devices grouped by category.
    """
    lights = {name: DEVICE_NAMES.get(name, name) for name in VALID_LIGHTS}
    outlets = {name: DEVICE_NAMES.get(name, name) for name in VALID_OUTLETS}

    return {
        "status": "ok",
        "lights": lights,
        "dimmer": {"svjetlo_fotelja": "Fotelja (dimmer) - brightness 0-255"},
        "outlets": outlets,
        "scenes": [
            "sve_ugasi - Ugasi sve (osim frižidera i bojlera)",
            "nocno - Noćni režim (hodnik + fotelja 25%)",
            "film - Filmski režim (TV + fotelja 25%, ostalo OFF)",
            "dolazak - Ulaz + hodnik + boravak + vani",
            "odlazak - Sve ugašeno (osim frižidera i bojlera)",
            "kuhanje - Kuhinja + šank + blagavaona",
        ],
        "notes": [
            "Kupaona <-> Bojler interlock: paljenje kupaone automatski gasi bojler",
            "PIR senzori: ručno paljenje blokira automatsko gašenje",
            "Zaštićeno (traži confirm=True): frižider OFF, bojler OFF, pećnica ON",
        ],
        "total": f"{len(VALID_LIGHTS)} svjetala + 1 dimmer + {len(VALID_OUTLETS)} utičnica = {len(VALID_SWITCHES) + 1} uređaja",
    }


def get_mqtt_adk_tools() -> list:
    """Get all MQTT smart home tools as a list"""
    return [
        mqtt_switch_control,
        mqtt_dimmer_control,
        mqtt_scene_control,
        mqtt_get_status,
        mqtt_list_devices,
    ]
