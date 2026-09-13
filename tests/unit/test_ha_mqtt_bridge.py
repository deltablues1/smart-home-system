from services.ha_mqtt_bridge import HomeAssistantMqttBridge


def test_build_discovery_messages_exposes_expected_entities(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    monkeypatch.setenv("HA_MQTT_STATE_PREFIX", "google_clause/rpi_voice")

    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
    )

    messages = dict(bridge.build_discovery_messages())

    assert any("/binary_sensor/" in topic for topic in messages)
    assert any("/select/" in topic for topic in messages)
    assert any("_last_transcript/config" in topic for topic in messages)
    assert any("_last_response/config" in topic for topic in messages)

    select_payload = next(payload for topic, payload in messages.items() if "/select/" in topic)
    assert select_payload["state_topic"] == "google_clause/rpi_voice/voice_mode/state"
    assert select_payload["command_topic"] == "google_clause/rpi_voice/voice_mode/set"
    assert select_payload["options"] == ["agent", "live"]


def test_mode_command_callback_runs_for_supported_modes(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    commands = []

    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
        mode_command_callback=commands.append,
    )

    class Message:
        topic = bridge.voice_mode_command_topic
        payload = b"live"

    bridge._on_message(None, None, Message())

    assert commands == ["live"]


# --- staying reachable ------------------------------------------------------


class _RecordingClient:
    def __init__(self):
        self.subscribed = []
        self.published = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


def test_every_connect_replays_discovery_and_the_last_states(monkeypatch):
    """The broker may have restarted, or never have heard us in the first place."""
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
    )
    client = _RecordingClient()
    bridge.client = client
    bridge.update_online(True)
    bridge.update_voice_mode("agent")
    client.published.clear()

    bridge._on_connect(client, None, None, 0)

    assert bridge.voice_mode_command_topic in client.subscribed
    assert any("/config" in topic for topic, _, _ in client.published)
    assert (bridge.availability_topic, "online", True) in client.published
    assert (bridge.voice_mode_state_topic, "agent", True) in client.published


def test_a_broker_that_never_answers_does_not_take_the_service_down(monkeypatch):
    """The wake-word service used to die here with [Errno 113] No route to host."""
    import paho.mqtt.client as mqtt_client

    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
    )

    class _RefusingClient(_RecordingClient):
        def will_set(self, *args, **kwargs):
            pass

        def username_pw_set(self, *args, **kwargs):
            pass

        def connect_async(self, *args, **kwargs):
            raise OSError(113, "No route to host")

        def loop_start(self):
            pass

    monkeypatch.setattr(
        mqtt_client, "Client", lambda *args, **kwargs: _RefusingClient()
    )

    bridge.start(initial_voice_mode="agent")  # must not raise

    assert bridge.client is None


# --- the microphone switch --------------------------------------------------


def test_discovery_exposes_the_listening_switch(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    monkeypatch.setenv("HA_MQTT_STATE_PREFIX", "google_clause/rpi_voice")

    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
    )

    messages = dict(bridge.build_discovery_messages())
    switch = next(payload for topic, payload in messages.items() if "/switch/" in topic)

    assert switch["state_topic"] == "google_clause/rpi_voice/listening/state"
    assert switch["command_topic"] == "google_clause/rpi_voice/listening/set"
    assert switch["payload_on"] == "ON"
    assert switch["payload_off"] == "OFF"


def test_listening_command_callback_maps_on_and_off(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    commands = []

    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
        listening_command_callback=commands.append,
    )

    class Message:
        topic = bridge.listening_command_topic

        def __init__(self, payload):
            self.payload = payload

    bridge._on_message(None, None, Message(b"ON"))
    bridge._on_message(None, None, Message(b"off"))
    bridge._on_message(None, None, Message(b"maybe"))

    assert commands == [True, False]


def test_connect_subscribes_and_replays_the_listening_state(monkeypatch):
    """HA restarting must not leave the switch showing a state we never sent."""
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    bridge = HomeAssistantMqttBridge(
        api_base_url="http://127.0.0.1:8000",
        voice_assistant_name="Jarvis",
    )
    client = _RecordingClient()
    bridge.client = client
    bridge.update_listening(False)
    client.published.clear()

    bridge._on_connect(client, None, None, 0)

    assert bridge.listening_command_topic in client.subscribed
    assert (bridge.listening_state_topic, "OFF", True) in client.published
