"""Tests for the read-only Home Assistant insight tools.

The user asked for Jarvis to know everything Home Assistant knows. The same
token that reads the logbook can unlock a door, so the first thing pinned here
is that this module only reads; the rest is about answers being right: the
right room, local times, and summaries that say what happened.

Run with:
    pytest tests/unit/test_ha_insight_tools.py -v
"""

import inspect
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from tools.adk_tools import ha_insight_tools as hi


def _s(entity_id, state, name, changed="2026-09-13T10:04:00+00:00", **attrs):
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": changed,
        "last_updated": changed,
        "attributes": {"friendly_name": name, **attrs},
    }


STATES = [
    _s("switch.esp32_io_svjetlo_kupaona", "on", "Svjetlo kupaona"),
    _s("switch.esp32_io_uticnica_bojler", "off", "Bojler"),
    _s("light.fotelja", "on", "Fotelja"),
    _s("sensor.kupaona_temperatura", "24.1", "Kupaona temperatura",
       unit_of_measurement="°C", device_class="temperature", state_class="measurement"),
    _s("sensor.potrosnja_kuca", "12.5", "Potrošnja kuća",
       unit_of_measurement="kWh", device_class="energy", state_class="total_increasing"),
    _s("sensor.potrosnja_kat", "6.1", "Potrošnja kat",
       unit_of_measurement="kWh", device_class="energy", state_class="total_increasing"),
    _s("binary_sensor.ulazna_vrata", "on", "Ulazna vrata", device_class="door"),
    _s("media_player.tv", "unavailable", "TV"),
    # A button has no state until pressed; it is not a dead device.
    _s("button.esp32_io_restart", "unknown", "ESP32 IO Restart"),
    _s("update.core", "on", "Home Assistant Core",
       installed_version="2026.8.3", latest_version="2026.9.1"),
]

AREAS_TEXT = (
    "kupaona|Kupaona|switch.esp32_io_svjetlo_kupaona,switch.esp32_io_uticnica_bojler,"
    "sensor.kupaona_temperatura;;"
    "living_room|Dnevni boravak|light.fotelja,media_player.tv;;"
    "ulaz|Ulaz|binary_sensor.ulazna_vrata;;"
)


@pytest.fixture(autouse=True)
def fake_ha(monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Zagreb")
    extra = {}

    def fake_get(path, timeout=15.0):
        if path == "/api/states":
            return STATES
        if path.startswith("/api/states/"):
            wanted = urllib.parse.unquote(path.split("/api/states/", 1)[1])
            for entry in STATES:
                if entry["entity_id"] == wanted:
                    return entry
            raise RuntimeError("HA API 404: Entity not found")
        for prefix, value in extra.items():
            if path.startswith(prefix):
                return value
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(hi, "_get", fake_get)
    monkeypatch.setattr(hi, "_render_template", lambda template, timeout=15.0: AREAS_TEXT)
    return extra


class TestItOnlyReads:
    def test_the_source_has_no_way_to_change_anything(self):
        source = inspect.getsource(hi)
        assert "/api/services" not in source
        assert "call_service" not in source
        # The one POST is the template renderer.
        assert source.count('method="POST"') == 1
        assert '"{url}/api/template"' in source.replace("f\"", "\"")

    def test_every_allowed_websocket_command_is_a_read(self):
        for command in hi._READ_ONLY_WS_COMMANDS:
            assert not any(verb in command for verb in (
                "call", "set", "create", "update", "delete", "remove", "save", "clear",
            )), command

    def test_a_write_command_is_refused_before_any_connection(self, monkeypatch):
        monkeypatch.delenv("HA_URL", raising=False)
        with pytest.raises(RuntimeError, match="nije dopuštena"):
            hi._ws([{"type": "call_service", "domain": "lock", "service": "unlock"}])


class TestHouseOverview:
    def test_one_room(self):
        result = hi.ha_house_overview("kupaona")
        (room,) = result["prostorije"]
        assert room["prostorija"] == "Kupaona"
        assert room["broj_entiteta"] == 3
        assert room["aktivno"] == ["Svjetlo kupaona"]

    def test_the_whole_house_names_what_is_on_open_and_dead(self):
        result = hi.ha_house_overview()
        rooms = {r["prostorija"]: r for r in result["prostorije"]}
        assert rooms["Ulaz"]["aktivno"] == ["Ulazna vrata"]
        assert rooms["Dnevni boravak"]["nedostupno"] == ["TV"]
        assert "Fotelja" in rooms["Dnevni boravak"]["aktivno"]
        assert result["ukupno_nedostupno"] == 1

    def test_an_unknown_room_lists_the_real_ones(self):
        result = hi.ha_house_overview("garaža")
        assert "error" in result
        assert "Kupaona" in result["prostorije"]


class TestFindEntities:
    def test_filters_combine(self):
        result = hi.ha_find_entities(domain="switch", state="on")
        assert [e["entity_id"] for e in result["entiteti"]] == ["switch.esp32_io_svjetlo_kupaona"]

    def test_room_and_local_time_are_attached(self):
        (entity,) = hi.ha_find_entities(query="bojler")["entiteti"]
        assert entity["podrucje"] == "Kupaona"
        assert entity["promijenjeno"] == "2026-09-13 12:04"  # 10:04 UTC is 12:04 in Zagreb

    def test_search_ignores_diacritics(self):
        result = hi.ha_find_entities(query="potrosnja")
        assert result["ukupno_pogodaka"] == 2

    def test_a_template_failure_does_not_hide_the_entities(self, monkeypatch):
        def broken(template, timeout=15.0):
            raise RuntimeError("no template")
        monkeypatch.setattr(hi, "_render_template", broken)
        assert hi.ha_find_entities(query="bojler")["ukupno_pogodaka"] == 1


class TestEntityDetails:
    def test_details_include_attributes_and_room(self):
        result = hi.ha_entity_details("sensor.kupaona_temperatura")
        assert result["atributi"]["unit_of_measurement"] == "°C"
        assert result["podrucje"] == "Kupaona"

    def test_unknown_entity(self):
        assert "nema entitet" in hi.ha_entity_details("sensor.nepostoji")["error"]

    def test_a_name_instead_of_an_id_points_to_the_search(self):
        assert "ha_find_entities" in hi.ha_entity_details("bojler")["error"]


class TestStateHistory:
    def test_on_off_history_adds_up_the_time_on(self, fake_ha):
        now = datetime.now(timezone.utc)
        iso = lambda delta: (now - delta).isoformat()
        fake_ha["/api/history/period/"] = [[
            {"entity_id": "switch.esp32_io_uticnica_bojler", "state": "off", "last_changed": iso(timedelta(hours=5))},
            {"state": "on", "last_changed": iso(timedelta(hours=3))},
            {"state": "off", "last_changed": iso(timedelta(hours=2))},
            {"state": "on", "last_changed": iso(timedelta(minutes=30))},
        ]]
        result = hi.ha_state_history("switch.esp32_io_uticnica_bojler", hours=4)
        summary = result["sazetak"]
        assert summary["broj_ukljucivanja"] == 2
        assert summary["ukupno_ukljuceno_min"] == pytest.approx(90, abs=1)
        assert summary["sada"] == "on"
        assert len(result["promjene"]) == 4

    def test_numeric_history_reports_the_range(self, fake_ha):
        now = datetime.now(timezone.utc)
        fake_ha["/api/history/period/"] = [[
            {"state": "22.0", "last_changed": (now - timedelta(hours=3)).isoformat()},
            {"state": "25.5", "last_changed": (now - timedelta(hours=2)).isoformat()},
            {"state": "23.1", "last_changed": (now - timedelta(hours=1)).isoformat()},
        ]]
        summary = hi.ha_state_history("sensor.kupaona_temperatura", hours=4)["sazetak"]
        assert (summary["najmanje"], summary["najvise"], summary["zadnje"]) == (22.0, 25.5, 23.1)


class TestLogbook:
    def test_newest_first_with_local_times_and_search(self, fake_ha):
        fake_ha["/api/logbook/"] = [
            {"name": "Svjetlo kupaona", "state": "on", "entity_id": "switch.a", "when": "2026-09-13T08:00:00+00:00"},
            {"name": "Bojler", "state": "off", "entity_id": "switch.b", "when": "2026-09-13T09:00:00+00:00"},
            {"name": "Svjetlo kupaona", "state": "off", "entity_id": "switch.a", "when": "2026-09-13T09:30:00+00:00"},
        ]
        result = hi.ha_logbook(hours=6, search="kupaona")
        assert result["ukupno"] == 2
        assert result["dogadaji"][0]["vrijeme"] == "2026-09-13 11:30"
        assert result["dogadaji"][0]["dogadaj"] == "→ off"


class TestStatistics:
    ROWS = [
        {"start": 1789164000000, "change": 4.5},
        {"start": 1789250400000, "change": 12.65},
    ]

    def test_consumption_by_name_sums_the_days(self, monkeypatch):
        seen = {}

        def fake_ws(commands):
            seen["commands"] = commands
            return [{"sensor.potrosnja_kuca": self.ROWS}]

        monkeypatch.setattr(hi, "_ws", fake_ws)
        result = hi.ha_statistics("potrosnja kuca", days=2)
        assert result["entity_id"] == "sensor.potrosnja_kuca"
        assert result["ukupno_potroseno"] == pytest.approx(17.15)
        assert [r["potroseno"] for r in result["redci"]] == [4.5, 12.65]
        assert result["jedinica"] == "kWh"
        assert seen["commands"][0]["type"] == "recorder/statistics_during_period"

    def test_an_ambiguous_name_asks_which_one(self, monkeypatch):
        monkeypatch.setattr(hi, "_ws", lambda commands: pytest.fail("must not query"))
        result = hi.ha_statistics("potrosnja")
        assert {c["entity_id"] for c in result["kandidati"]} == {
            "sensor.potrosnja_kuca", "sensor.potrosnja_kat",
        }

    def test_a_measurement_reports_min_max_mean_not_consumption(self, monkeypatch):
        monkeypatch.setattr(hi, "_ws", lambda commands: [{
            "sensor.kupaona_temperatura": [{"start": 1789250400000, "min": 21.0, "max": 26.0, "mean": 23.4}],
        }])
        result = hi.ha_statistics("sensor.kupaona_temperatura", days=1)
        row = result["redci"][0]
        assert (row["najmanje"], row["najvise"], row["prosjek"]) == (21.0, 26.0, 23.4)
        assert "ukupno_potroseno" not in result


LOG = [
    {"name": "homeassistant.components.esphome", "level": "WARNING", "message": ["slow"], "count": 242,
     "timestamp": 1789291868.0, "first_occurred": 1789200000.0},
    {"name": "custom_components.jarvis", "level": "ERROR", "message": ["Jarvis unreachable"], "count": 3,
     "timestamp": 1789300000.0, "first_occurred": 1789290000.0},
]


class TestSystemLog:
    def test_newest_first_and_level_filter(self, monkeypatch):
        monkeypatch.setattr(hi, "_ws", lambda commands: [LOG])
        everything = hi.ha_system_log()
        assert everything["zapisi"][0]["izvor"] == "custom_components.jarvis"
        errors = hi.ha_system_log(level="error")
        assert errors["ukupno"] == 1
        assert errors["zapisi"][0]["puta"] == 3


class TestSystemHealth:
    def test_it_names_the_problems(self, monkeypatch):
        entries = [
            {"domain": "esphome", "title": "ESP32 IO", "state": "loaded"},
            {"domain": "tailscale", "title": "Tailscale", "state": "setup_retry"},
            {"domain": "demo", "title": "Demo", "state": "not_loaded", "disabled_by": "user"},
        ]
        monkeypatch.setattr(hi, "_ws", lambda commands: [{"version": "2026.8.3"}, entries, LOG])
        result = hi.ha_system_health()
        assert result["verzija"] == "2026.8.3"
        assert result["sve_u_redu"] is False
        assert [b["integracija"] for b in result["integracije_s_problemom"]] == ["tailscale"]
        assert result["nedostupni_entiteti"] == ["TV"]
        assert result["azuriranja"][0]["dostupno"] == "2026.9.1"
        assert result["log_po_razini"] == {"WARNING": 1, "ERROR": 1}
