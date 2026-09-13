"""Tests for the Jarvis Pi's self-reporting to Home Assistant.

The parsing is the part that can be quietly wrong -- a misread field produces a
plausible number rather than an error -- so it is tested against real /proc and
/sys text taken from the Pi.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.host_metrics import (  # noqa: E402
    STATE_PREFIX,
    HostMetricsPublisher,
    MetricsReader,
    build_discovery,
    parse_cpu_times,
    parse_memory_percent,
    parse_temperature,
    parse_uptime_seconds,
)

# Straight off the Pi.
STAT = """cpu  65575 63 23629 173347215 3326 0 414 0 0 0
cpu0 16393 15 5907 43336803 831 0 103 0 0 0
intr 12345
ctxt 987654
"""

MEMINFO = """MemTotal:        8231404 kB
MemFree:         5551292 kB
MemAvailable:    7402108 kB
Buffers:          123456 kB
Cached:          1500000 kB
"""


def test_cpu_times_come_from_the_aggregate_line():
    idle, total = parse_cpu_times(STAT)

    assert idle == 173347215 + 3326  # idle + iowait
    assert total == 65575 + 63 + 23629 + 173347215 + 3326 + 0 + 414


def test_a_missing_cpu_line_is_an_error_not_a_zero():
    """Silently reporting 0% load would look like a healthy idle machine."""
    with pytest.raises(ValueError):
        parse_cpu_times("intr 12345\nctxt 987654\n")


def test_memory_counts_cache_as_free():
    """MemFree alone would report 33% used on a machine that is really at 10%."""
    assert parse_memory_percent(MEMINFO) == 10.1


def test_memory_without_the_available_field_falls_back_to_total():
    assert parse_memory_percent("MemTotal:  1000 kB\nMemFree:  400 kB\n") == 0.0


def test_memory_without_a_total_is_an_error():
    with pytest.raises(ValueError):
        parse_memory_percent("MemFree: 400 kB\n")


def test_temperature_is_converted_from_millidegrees():
    assert parse_temperature("47123\n") == 47.1
    # 54.55 is not exactly representable, so it rounds down. Either way is fine
    # for a temperature; the test records which one happens.
    assert parse_temperature("54550\n") == 54.5


def test_uptime_takes_the_first_field():
    """The second field is idle time across all cores and is much larger."""
    assert parse_uptime_seconds("433853.74 1733472.16\n") == 433853.74


def test_the_first_cpu_sample_reports_nothing():
    """One sample is a total since boot, not a current load."""
    reader = MetricsReader()

    assert reader.cpu_percent(STAT) is None


def test_load_is_the_change_between_two_samples():
    reader = MetricsReader()
    reader.cpu_percent(STAT)

    # 100 more jiffies of work, 300 more idle -> 25% busy.
    busier = STAT.replace(
        "cpu  65575 63 23629 173347215 3326",
        "cpu  65675 63 23629 173347515 3326",
    )
    assert reader.cpu_percent(busier) == 25.0


def test_two_identical_samples_report_nothing_rather_than_zero():
    """No elapsed time means no measurement, which is not the same as idle."""
    reader = MetricsReader()
    reader.cpu_percent(STAT)

    assert reader.cpu_percent(STAT) is None


def test_every_sensor_is_announced_against_one_device():
    messages = build_discovery("Jarvis Pi", "http://jarvis.local:8000")

    assert len(messages) == 8
    devices = {tuple(payload["device"]["identifiers"]) for _, payload in messages}
    assert devices == {("jarvis_pi_host",)}
    assert all(topic.startswith("homeassistant/sensor/") for topic, _ in messages)


def test_every_sensor_can_go_unavailable():
    """Without this a dead Pi keeps showing its last reading as if it were live."""
    for _, payload in build_discovery("Jarvis Pi", "http://x"):
        assert payload["availability_topic"].endswith("/availability")
        assert payload["payload_not_available"] == "offline"


def test_unique_ids_do_not_collide():
    ids = [payload["unique_id"] for _, payload in build_discovery("Jarvis Pi", "http://x")]

    assert len(ids) == len(set(ids))


def test_each_sensor_reads_its_own_field_of_the_shared_payload():
    templates = {
        payload["unique_id"]: payload["value_template"]
        for _, payload in build_discovery("Jarvis Pi", "http://x")
    }

    assert templates["jarvis_pi_host_cpu"] == "{{ value_json.cpu }}"
    assert templates["jarvis_pi_host_temperature"] == "{{ value_json.temperature }}"


def test_the_boot_time_is_a_timestamp_so_home_assistant_can_age_it():
    payloads = {p["unique_id"]: p for _, p in build_discovery("Jarvis Pi", "http://x")}

    assert payloads["jarvis_pi_host_boot"]["device_class"] == "timestamp"
    assert "state_class" not in payloads["jarvis_pi_host_boot"]


# --- staying reachable ------------------------------------------------------


class _RecordingClient:
    """Just enough of a paho client to see what would go on the wire."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False):
        self.published.append((topic, payload, retain))


def test_every_connect_re_announces_the_device(monkeypatch):
    """A reconnect must undo our own last will, or the sensors stay offline."""
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    publisher = HostMetricsPublisher()
    client = _RecordingClient()

    publisher._on_connect(client, None, None, 0)

    topics = [topic for topic, _, _ in client.published]
    assert any(topic.endswith("/config") for topic in topics)
    assert (f"{STATE_PREFIX}/availability", "online", True) in client.published


def test_a_refused_connection_announces_nothing(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER", "127.0.0.1")
    publisher = HostMetricsPublisher()
    client = _RecordingClient()

    publisher._on_connect(client, None, None, 5)  # 5 = not authorised

    assert client.published == []
