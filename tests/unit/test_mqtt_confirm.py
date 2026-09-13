"""
Unit tests for services/mqtt_confirm.py — publish-and-confirm with a fake
paho client. No broker, no network.

Covers: full confirmation, timeout, partial scenes, JSON dimmer states,
confirm-disabled fallback, degraded blind publish, broker-down error.

Run with:
    pytest tests/unit/test_mqtt_confirm.py -v
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

import services.mqtt_confirm as mqtt_confirm
from services.mqtt_confirm import DeviceCommand, expect_json_state, publish_and_confirm


class FakeMqttClient:
    """Fake paho client. echo_topics: command topics whose devices 'reply'
    by publishing the payload on the matching /state topic. retained: state
    topics whose (possibly stale) value the broker replays on subscribe."""

    def __init__(self, echo_topics=None, subscribe_raises=False, retained=None):
        self.published = []
        self.subscribed = []
        self.on_message = None
        self.echo_topics = echo_topics  # None = echo everything
        self.subscribe_raises = subscribe_raises
        self.retained = retained or {}

    def subscribe(self, topic, qos=0):
        if self.subscribe_raises:
            raise RuntimeError("subscribe failed")
        self.subscribed.append(topic)
        if topic in self.retained and self.on_message:
            msg = SimpleNamespace(
                topic=topic, payload=str(self.retained[topic]).encode("utf-8")
            )
            self.on_message(self, None, msg)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))
        if self.on_message is None:
            return
        if self.echo_topics is not None and topic not in self.echo_topics:
            return
        state_topic = topic.replace("/command", "/state")
        if state_topic in self.subscribed:
            msg = SimpleNamespace(
                topic=state_topic,
                payload=str(payload).encode("utf-8"),
            )
            self.on_message(self, None, msg)

    def disconnect(self):
        pass


def _switch_cmd(name, state="ON"):
    return DeviceCommand(
        name=name,
        command_topic=f"esp32-io/switch/{name}/command",
        payload=state,
        state_topic=f"esp32-io/switch/{name}/state",
        expected=state,
    )


@pytest.fixture(autouse=True)
def fast_timeout(monkeypatch):
    monkeypatch.setenv("MQTT_CONFIRM_TIMEOUT", "0.2")
    monkeypatch.delenv("MQTT_CONFIRM_ENABLED", raising=False)


def _use_client(monkeypatch, client):
    monkeypatch.setattr(mqtt_confirm, "_client_factory", lambda: client)
    return client


class TestFullConfirmation:
    def test_single_device_confirmed(self, monkeypatch):
        client = _use_client(monkeypatch, FakeMqttClient())

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja")]))

        assert result["status"] == "confirmed"
        assert result["confirmed"] == 1 and result["total"] == 1
        assert result["devices"]["svjetlo_kuhinja"]["observed"] == "ON"
        assert len(result["operation_id"]) == 8
        assert ("esp32-io/switch/svjetlo_kuhinja/command", "ON") in client.published

    def test_dimmer_json_state(self, monkeypatch):
        _use_client(monkeypatch, FakeMqttClient())
        cmd = DeviceCommand(
            name="svjetlo_fotelja",
            command_topic="esp32-io/light/svjetlo_fotelja/command",
            payload=json.dumps({"state": "ON", "brightness": 64}),
            state_topic="esp32-io/light/svjetlo_fotelja/state",
            expected=expect_json_state("ON"),
        )

        result = asyncio.run(publish_and_confirm([cmd]))
        assert result["status"] == "confirmed"


class TestTimeoutAndPartial:
    def test_no_echo_times_out(self, monkeypatch):
        _use_client(monkeypatch, FakeMqttClient(echo_topics=[]))

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja")]))

        assert result["status"] == "timeout"
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "timeout"

    def test_partial_scene(self, monkeypatch):
        echo_only = ["esp32-io/switch/svjetlo_kuhinja/command"]
        _use_client(monkeypatch, FakeMqttClient(echo_topics=echo_only))

        result = asyncio.run(publish_and_confirm([
            _switch_cmd("svjetlo_kuhinja"),
            _switch_cmd("svjetlo_hodnik"),
        ]))

        assert result["status"] == "partial"
        assert result["confirmed"] == 1 and result["total"] == 2
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "confirmed"
        assert result["devices"]["svjetlo_hodnik"]["status"] == "timeout"

    def test_retained_expected_is_already_in_state_not_confirmed(self, monkeypatch):
        # Broker retains "ON" (device possibly OFFLINE), no echo after publish:
        # must NOT count as "confirmed" — it becomes "already_in_state".
        _use_client(monkeypatch, FakeMqttClient(
            echo_topics=[],
            retained={"esp32-io/switch/svjetlo_kuhinja/state": "ON"},
        ))

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))

        assert result["devices"]["svjetlo_kuhinja"]["status"] == "already_in_state"
        # And the aggregate says the same thing. It used to fold this into
        # "confirmed" — the test name already said that was wrong, and the
        # assertion recorded it anyway. On 2026-09-06 that collapse is what
        # let a light report success for three days after its ESP32 dropped
        # off MQTT, because the broker kept serving the stale retained "ON".
        assert result["status"] == "already_in_state"

    def test_baseline_match_invalidated_by_contradicting_echo(self, monkeypatch):
        # THE review bug: broker retains "ON" (baseline matches), but after
        # the publish the device reports "OFF". The stale baseline must NOT
        # produce a success — the device is observably NOT in the target state.
        client = FakeMqttClient(
            echo_topics=[],
            retained={"esp32-io/switch/svjetlo_kuhinja/state": "ON"},
        )
        _use_client(monkeypatch, client)

        original_publish = client.publish

        def publish_contradicting_echo(topic, payload, qos=0):
            original_publish(topic, payload, qos)
            state_topic = topic.replace("/command", "/state")
            if state_topic in client.subscribed and client.on_message:
                msg = SimpleNamespace(topic=state_topic, payload=b"OFF", retain=False)
                client.on_message(client, None, msg)

        client.publish = publish_contradicting_echo

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))

        assert result["status"] == "timeout"
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "timeout"
        assert result["devices"]["svjetlo_kuhinja"]["observed"] == "OFF"

    def test_late_retained_replay_not_counted_as_echo(self, monkeypatch):
        # A retained packet arriving AFTER publish (marked retain=True) is
        # still baseline — it must not confirm the command.
        client = FakeMqttClient(echo_topics=[])
        _use_client(monkeypatch, client)

        original_publish = client.publish

        def publish_late_retained(topic, payload, qos=0):
            original_publish(topic, payload, qos)
            state_topic = topic.replace("/command", "/state")
            if state_topic in client.subscribed and client.on_message:
                msg = SimpleNamespace(topic=state_topic, payload=b"ON", retain=True)
                client.on_message(client, None, msg)

        client.publish = publish_late_retained

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))
        # baseline-only match => already_in_state, never "confirmed"
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "already_in_state"

    def test_retained_wrong_state_times_out(self, monkeypatch):
        # Broker retains stale "OFF" while we request ON and no echo arrives.
        _use_client(monkeypatch, FakeMqttClient(
            echo_topics=[],
            retained={"esp32-io/switch/svjetlo_kuhinja/state": "OFF"},
        ))

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))

        assert result["status"] == "timeout"
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "timeout"

    def test_dimmer_brightness_mismatch_not_confirmed(self, monkeypatch):
        # Lamp echoes ON at 255 while we asked for 64 — must not confirm.
        client = FakeMqttClient(echo_topics=[])
        _use_client(monkeypatch, client)

        original_publish = client.publish

        def publish_wrong_brightness(topic, payload, qos=0):
            original_publish(topic, payload, qos)
            state_topic = topic.replace("/command", "/state")
            if state_topic in client.subscribed and client.on_message:
                msg = SimpleNamespace(
                    topic=state_topic,
                    payload=json.dumps({"state": "ON", "brightness": 255}).encode(),
                )
                client.on_message(client, None, msg)

        client.publish = publish_wrong_brightness

        cmd = DeviceCommand(
            name="svjetlo_fotelja",
            command_topic="esp32-io/light/svjetlo_fotelja/command",
            payload=json.dumps({"state": "ON", "brightness": 64}),
            state_topic="esp32-io/light/svjetlo_fotelja/state",
            expected=expect_json_state("ON", brightness=64),
        )
        result = asyncio.run(publish_and_confirm([cmd]))
        assert result["status"] == "timeout"

    def test_dimmer_brightness_within_tolerance_confirmed(self):
        match = expect_json_state("ON", brightness=64, tolerance=10)
        assert match(json.dumps({"state": "ON", "brightness": 70})) is True
        assert match(json.dumps({"state": "ON", "brightness": 90})) is False
        assert match(json.dumps({"state": "ON"})) is False

    def test_wrong_state_echo_not_confirmed(self, monkeypatch):
        # Device echoes OFF (e.g. retained old state) while we expect ON.
        client = FakeMqttClient(echo_topics=[])
        _use_client(monkeypatch, client)

        original_publish = client.publish

        def publish_wrong_echo(topic, payload, qos=0):
            original_publish(topic, payload, qos)
            state_topic = topic.replace("/command", "/state")
            if state_topic in client.subscribed and client.on_message:
                msg = SimpleNamespace(topic=state_topic, payload=b"OFF")
                client.on_message(client, None, msg)

        client.publish = publish_wrong_echo

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))
        assert result["status"] == "timeout"
        assert result["devices"]["svjetlo_kuhinja"]["observed"] == "OFF"


class TestFallbacks:
    def test_confirm_disabled_blind_publish(self, monkeypatch):
        monkeypatch.setenv("MQTT_CONFIRM_ENABLED", "false")
        client = _use_client(monkeypatch, FakeMqttClient())

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja")]))

        # "sent" — published fire-and-forget, explicitly NOT confirmed
        assert result["status"] == "sent"
        assert result["devices"]["svjetlo_kuhinja"]["status"] == "unconfirmed"
        assert len(client.published) == 1

    def test_subscribe_failure_degrades_to_blind_publish(self, monkeypatch):
        client = _use_client(monkeypatch, FakeMqttClient(subscribe_raises=True))

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja")]))

        assert result["status"] == "unconfirmed"
        assert "warning" in result
        # Action was still executed via blind publish
        assert ("esp32-io/switch/svjetlo_kuhinja/command", "ON") in client.published

    def test_broker_down_returns_error(self, monkeypatch):
        def broken_factory():
            raise ConnectionRefusedError("broker down")

        monkeypatch.setattr(mqtt_confirm, "_client_factory", broken_factory)

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja")]))
        assert result["status"] == "error"


class TestVoiceOutcomeResponses:
    def test_timeout_always_spoken_even_in_none_mode(self, monkeypatch):
        import services.voice_fast_path as vfp

        async def fake_switch(device_name, state):
            return {"status": "timeout", "device": device_name}

        monkeypatch.setattr(vfp, "mqtt_switch_control", fake_switch)
        monkeypatch.setenv("VOICE_SMART_HOME_RESPONSE_MODE", "none")

        result = asyncio.run(
            vfp.execute_fast_smart_home_command("Upali svjetlo u kuhinji")
        )
        assert result is not None
        assert "nije potvrdio" in result

    def test_partial_scene_spoken(self, monkeypatch):
        import services.voice_fast_path as vfp

        async def fake_scene(scene):
            return {
                "status": "partial", "description": "Filmski režim",
                "confirmed": 3, "total": 5,
                "unconfirmed_devices": ["Kuhinja", "Hodnik"],
            }

        monkeypatch.setattr(vfp, "mqtt_scene_control", fake_scene)

        result = asyncio.run(vfp.execute_fast_smart_home_command("idemo gledati film"))
        assert result is not None
        assert "3 od 5" in result

    def test_error_spoken(self, monkeypatch):
        import services.voice_fast_path as vfp

        async def fake_scene(scene):
            return {"status": "error", "error": "no broker"}

        monkeypatch.setattr(vfp, "mqtt_scene_control", fake_scene)

        result = asyncio.run(vfp.execute_fast_smart_home_command("ugasi sve"))
        assert result is not None
        assert "MQTT" in result or "pametnu kuću" in result


class TestTheAggregateKeepsTheDistinction:
    """"Confirmed" has to mean a device said something.

    The per-device layer separates an echo from a stale baseline and calls the
    second a weaker signal. Summing them under the strong word threw that away,
    and a house whose controller had silently left the broker answered "u redu"
    to every command for three days.
    """

    def test_all_echoed_is_confirmed(self, monkeypatch):
        _use_client(monkeypatch, FakeMqttClient(
            echo_topics=["esp32-io/switch/svjetlo_kuhinja/command"],
        ))

        result = asyncio.run(publish_and_confirm([_switch_cmd("svjetlo_kuhinja", "ON")]))

        assert result["status"] == "confirmed"

    def test_all_baseline_is_not_confirmed(self, monkeypatch):
        _use_client(monkeypatch, FakeMqttClient(
            echo_topics=[],
            retained={
                "esp32-io/switch/svjetlo_kuhinja/state": "ON",
                "esp32-io/switch/svjetlo_hodnik/state": "ON",
            },
        ))

        result = asyncio.run(publish_and_confirm([
            _switch_cmd("svjetlo_kuhinja", "ON"),
            _switch_cmd("svjetlo_hodnik", "ON"),
        ]))

        assert result["status"] == "already_in_state"
        assert result["confirmed"] == 2  # nothing was refused

    def test_one_echo_one_baseline_is_not_confirmed(self, monkeypatch):
        _use_client(monkeypatch, FakeMqttClient(
            echo_topics=["esp32-io/switch/svjetlo_kuhinja/command"],
            retained={"esp32-io/switch/svjetlo_hodnik/state": "ON"},
        ))

        result = asyncio.run(publish_and_confirm([
            _switch_cmd("svjetlo_kuhinja", "ON"),
            _switch_cmd("svjetlo_hodnik", "ON"),
        ]))

        # One device told us; the other did not. That is not a confirmed set.
        assert result["status"] == "already_in_state"
