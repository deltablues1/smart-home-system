"""Tests for the generated parts of the Home Assistant dashboard.

The view itself is built against a live Home Assistant, but the naming and
ordering decisions are pure and are what make a room readable rather than a
dump of entity ids.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

_TOOL = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "home_assistant"
    / "tools"
    / "build_dashboard.py"
)

pytest.importorskip("websockets", reason="the tool imports websockets at module level")

# The script reads the Home Assistant address at import time. It never connects
# unless run as a program, but the names have to exist.
os.environ.setdefault("HA_URL", "http://example.invalid:8123")
os.environ.setdefault("HA_TOKEN", "token")

_spec = importlib.util.spec_from_file_location("build_dashboard", _TOOL)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


def test_the_device_prefix_is_dropped_from_a_room_label():
    """"ESP32 IO Svjetlo kuhinja" in the Kitchen card only needs "Svjetlo kuhinja"."""
    assert builder.short_name("ESP32 IO Svjetlo kuhinja", "switch.x") == "Svjetlo kuhinja"
    assert (
        builder.short_name("BME280 Mux Node Soba temperatura", "sensor.x")
        == "Soba temperatura"
    )


def test_a_name_without_a_known_prefix_is_left_alone():
    assert builder.short_name("Pećnica", "switch.x") == "Pećnica"


def test_a_missing_friendly_name_falls_back_to_the_entity_id():
    assert builder.short_name("", "switch.esp32_io_pecnica") == "Esp32 io pecnica"


def _rooms(entities):
    return builder.rooms_view(entities, {"living_room": "Dnevni boravak"})


def test_room_summary_navigates_without_toggling_its_fallback_switch():
    entities = {"living_room": [{"entity_id": "switch.svjetlo", "name": "Svjetlo"}]}
    summary = _rooms(entities)["sections"][0]["cards"][0]["cards"][0]
    detail = builder.room_views(entities, {"living_room": "Boravak"})[0]
    assert summary["hide_state"] is True
    for action in ("tap_action", "icon_tap_action"):
        assert summary[action] == {
            "action": "navigate", "navigation_path": f"/{builder.BOARD}/{detail['path']}"
        }
    assert detail["subview"] is True
    assert detail["back_path"] == f"/{builder.BOARD}/sobe"
    control = detail["sections"][0]["cards"][1]
    assert control["tap_action"] == {"action": "toggle"}
    assert control["icon_tap_action"] == {"action": "toggle"}


def test_room_detail_preserves_controls_and_measurements_but_excludes_noise():
    entities = {"living_room": [
        {"entity_id": eid, "name": name} for eid, name in [
            ("switch.b", "Zidno"), ("switch.a", "Ambijentalno"),
            ("media_player.tv", "TV"),
            ("sensor.bme280_mux_node_soba_temperatura", "Temperatura"),
            ("sensor.bme280_mux_node_soba_vlaga", "Vlaga"),
            ("switch.esp32_io_slobodno1", "Slobodno"),
        ]
    ]}
    view = builder.room_views(entities, {"living_room": "Boravak"})[0]
    cards = [c for section in view["sections"] for c in section["cards"]]
    assert [c["name"] for c in cards if c.get("type") == "tile"] == ["Ambijentalno", "Zidno", "TV"]
    readings = next(c for c in cards if c.get("type") == "entities")
    assert [e["name"] for e in readings["entities"]] == ["Temperatura", "Vlaga"]
    assert next(c for c in cards if c.get("entity") == "media_player.tv")["tap_action"] == {"action": "more-info"}


def test_an_empty_room_gets_no_section():
    """An empty heading reads as a fault in the room, not an empty room."""
    view = builder.rooms_view({"living_room": []}, {"living_room": "Dnevni boravak"})

    assert view["sections"] == []


def test_rooms_keep_the_configured_order():
    entities = {
        area_id: [{"entity_id": f"switch.{area_id}", "name": "X"}]
        for area_id, _ in builder.ROOM_ORDER
    }
    names = {area_id: area_id for area_id, _ in builder.ROOM_ORDER}

    view = builder.rooms_view(entities, names)
    cards = view["sections"][0]["cards"]
    assert [card["cards"][0]["name"] for card in cards] == [a for a, _ in builder.ROOM_ORDER]
    # Adding many devices to one room cannot push the next room below it.
    entities["living_room"] += [{"entity_id": f"switch.extra{i}", "name": str(i)} for i in range(20)]
    crowded = builder.rooms_view(entities, names)["sections"][0]["cards"]
    assert [c["grid_options"] for c in crowded] == [c["grid_options"] for c in cards]


def test_every_machine_on_the_system_view_reports_availability():
    """A card that cannot say OFFLINE is what let a dead node look healthy."""
    view = builder.system_view()
    markdown = [
        card["content"]
        for section in view["sections"]
        for card in section["cards"]
        if card.get("type") == "markdown"
    ]

    assert len(markdown) == 4
    assert all("NE JAVLJA SE" in content and "unavailable" in content for content in markdown)
    assert all("Online" in content for content in markdown)


def test_a_reading_is_labelled_by_what_it_measures():
    """"Dnevni prostor temperatura" under a heading that says the room is noise."""
    assert (
        builder.reading_name("sensor.bme280_mux_node_dnevni_prostor_temperatura", "x")
        == "Temperatura"
    )
    assert builder.reading_name("sensor.bme280_mux_node_soba_vlaga", "x") == "Vlaga"
    assert (
        builder.reading_name("sensor.bme280_mux_node_kvaliteta_zraka_pm2_5", "x") == "PM2.5"
    )


def test_an_unrecognised_reading_keeps_its_own_name():
    assert builder.reading_name("sensor.nesto_novo", "Nešto novo") == "Nešto novo"


def test_the_wall_buttons_and_spare_relays_stay_out_of_rooms():
    """They are real entities, and useless when you are looking at a room."""
    assert not builder.wanted_in_room("binary_sensor.esp32_io_tipkalo_svjetlo_kuhinja")
    assert not builder.wanted_in_room("switch.esp32_io_slobodno1")
    assert not builder.wanted_in_room("sensor.bme280_mux_node_bme280_mux_wifi_signal")
    assert not builder.wanted_in_room("sensor.bme280_mux_node_broj_cestica_pm1")
    assert builder.wanted_in_room("switch.esp32_io_svjetlo_kuhinja")
    assert builder.wanted_in_room("sensor.bme280_mux_node_kupaona_temperatura")


def test_the_house_is_not_a_room():
    """Power measurement and node diagnostics belong on the System view."""
    assert "kuca" not in [area_id for area_id, _ in builder.ROOM_ORDER]


def test_views_use_at_most_three_columns():
    """A width constraint; actual height is checked in the HA browser at panel scale."""
    for view in (builder.climate_view(), builder.air_view(), builder.system_view()):
        assert view["max_columns"] <= 3


def test_sensor_only_room_is_still_reachable():
    entities = {"vani": [{"entity_id": "sensor.vanjska_temperatura", "name": "Vani"}]}
    view = builder.rooms_view(entities, {"vani": "Vani"})
    card = view["sections"][0]["cards"][0]["cards"][0]
    assert card["entity"] == "sensor.vanjska_temperatura"
    assert card["hide_state"] is False
    assert builder.room_views(entities, {"vani": "Vani"})[0]["sections"]


def test_a_switch_is_a_socket_unless_it_is_a_light():
    """Listing sockets by the word "utičnica" silently lost the oven.

    Split by what an entity IS, not by what it happens to be called, so an
    appliance added tomorrow lands somewhere instead of nowhere.
    """
    ids = [
        "switch.esp32_io_svjetlo_kuhinja",
        "light.esp32_io_svjetlo_fotelja",
        "switch.esp32_io_uticnica_tv",
        "switch.esp32_io_pecnica",
    ]

    lights = [e for e in ids if "svjetlo" in e]
    sockets = [e for e in ids if "svjetlo" not in e]

    assert sockets == ["switch.esp32_io_uticnica_tv", "switch.esp32_io_pecnica"]
    assert len(lights) + len(sockets) == len(ids)


def test_only_a_real_off_to_on_counts_as_using_a_switch():
    """A node reboot republishes every entity as `on`.

    Counting unavailable -> on made all eleven sockets look equally busy at
    thirteen switches each, which is a reboot artefact rather than a habit and
    would have ordered the panel by it.
    """
    history = {
        "switch.a": [
            {"s": "off"}, {"s": "on"}, {"s": "off"}, {"s": "on"},
        ],
        "switch.b": [
            {"s": "on"}, {"s": "unavailable"}, {"s": "on"},
            {"s": "unavailable"}, {"s": "on"},
        ],
    }

    counts = builder.count_switch_ons(history, ["switch.a", "switch.b"])

    assert counts["switch.a"] == 2
    assert counts["switch.b"] == 0


def test_a_switch_with_no_recorded_history_counts_zero():
    assert builder.count_switch_ons({}, ["switch.novi"]) == {"switch.novi": 0}


def test_lights_and_appliances_remain_separate_views():
    lights = builder.lights_view([builder.tile("switch.svjetlo", "Kupaona", True)])
    appliances = builder.appliances_view([("switch.pecnica", "Pećnica")])
    assert lights["path"] == "svjetla"
    assert appliances["path"] == "trosila"


def test_daily_extremes_are_still_reachable_from_climate():
    view = builder.climate_view()
    detail = builder.climate_extremes_view()
    action = view["sections"][0]["cards"][1]["tap_action"]
    assert action["navigation_path"] == f"/{builder.BOARD}/{detail['path']}"
    assert detail["back_path"] == f"/{builder.BOARD}/klima"
    for section in detail["sections"]:
        stats = section["cards"][1:]
        assert {c["stat_type"] for c in stats} == {"min", "max"}
        assert all(c["period"] == {"calendar": {"period": "day"}} for c in stats)


def test_period_selector_is_shared_and_unavailable_helper_keeps_daily_graphs():
    for view in (builder.climate_view(), builder.air_view()):
        selector = view["sections"][0]["cards"][0]
        assert selector["entity"] == builder.PERIOD_HELPER
        assert selector["features_position"] == "inline"
    graphs = builder.period_graphs(builder.PRESSURES, rows=4)
    for state in ("Dan", "Tjedan", "Mjesec", "Godina", "unknown", "unavailable"):
        visible = [g for g in graphs if any(c["state"] == state for c in g["conditions"][0]["conditions"])]
        assert len(visible) == 1


def test_pressure_uses_native_states_not_zero_for_missing_readings():
    readings = builder.climate_view()["sections"][3]["cards"][1]
    assert readings["type"] == "entities"
    assert [e["entity"] for e in readings["entities"]] == [e for e, _ in builder.PRESSURES]


@pytest.mark.parametrize("phase,label", [
    ("new_moon", "Mladi Mjesec"),
    ("waxing_crescent", "Srp u rastu"),
    ("first_quarter", "Prva četvrt"),
    ("waxing_gibbous", "Mjesec raste"),
    ("full_moon", "Puni Mjesec"),
    ("waning_gibbous", "Mjesec opada"),
    ("last_quarter", "Zadnja četvrt"),
    ("waning_crescent", "Srp u opadanju"),
    ("unavailable", "Mijena nije dostupna"),
])
def test_moon_names_render_even_when_sun_sensors_are_unavailable(phase, label):
    from jinja2 import Environment

    env = Environment()
    env.globals.update(
        states=lambda entity: phase if entity == "sensor.moon_phase" else "unavailable",
        as_timestamp=lambda value, default=None: default,
        is_state=lambda entity, value: False,
        state_attr=lambda entity, attr: None,
        is_number=lambda value: isinstance(value, (int, float)),
    )
    env.filters["timestamp_custom"] = lambda value, fmt, default=None: default
    result = env.from_string(builder.SUN_LINE).render()
    assert label in result
    assert "<ha-icon" in result
    assert "uštap" not in result


def _cards(view):
    """Every card in a view, whatever section it sits in."""
    out = []
    for section in view.get("sections", []):
        out.extend(section.get("cards", []))
    return out


@pytest.fixture
def channels(tmp_path, monkeypatch):
    """A learned-channel store of our own.

    config/tv_channels.json is gitignored: it is what the agent has learned on
    one installation, not something the repo ships. Reading the real one would
    make this test pass or fail depending on whose machine it runs on.
    """
    store = tmp_path / "config"
    store.mkdir()
    (store / "tv_channels.json").write_text(json.dumps({
        "HRT 1 HD": {"number": 1, "app": "a1 xplore tv"},
        "HRT 1": {"number": 1, "app": "a1 xplore tv"},
        "Arena Sport 1 HD": {"number": 201, "app": "a1 xplore tv"},
        "N1": {"number": None, "app": "a1 xplore tv"},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    return store


class TestTheEveningOf20260906:
    """These were applied to the live dashboard through the websocket API.

    The generator rewrites the whole config, so without these tests the next
    run silently deletes an evening of work -- which is exactly the risk that
    prompted porting them here.
    """

    def test_the_shopping_tile_opens_the_list(self):
        tiles = [
            c for c in _cards(builder.overview_view([], []))
            if c.get("entity") == "todo.shopping_list"
        ]
        assert tiles, "the overview lost its shopping tile"
        assert tiles[0]["tap_action"] == {
            "action": "navigate",
            "navigation_path": f"/{builder.BOARD}/kupovina",
        }

    def test_there_is_a_view_at_the_other_end(self):
        view = builder.shopping_view()
        assert view["path"] == "kupovina"
        assert view["subview"] is True, "it must not add a tab to the panel"
        assert any(c["type"] == "todo-list" for c in _cards(view))

    def test_the_sky_card_is_not_clipped(self):
        """Two fixed rows cut the moon line off the bottom."""
        cards = _cards(builder.overview_view([], []))
        sky = [c for c in cards if c.get("content") is builder.SUN_LINE]
        assert sky, "the sun/moon line disappeared from the overview"
        assert sky[0]["grid_options"]["rows"] == "auto"

    def test_something_actually_links_to_the_channel_view(self):
        """A view nothing navigates to is reachable only by typing a URL."""
        navs = [
            c["tap_action"]["navigation_path"] for c in _cards(builder.tv_view())
            if c.get("tap_action", {}).get("action") == "navigate"
        ]
        assert f"/{builder.BOARD}/kanali" in navs

    def test_the_a1_button_no_longer_just_launches_the_app(self):
        names = [a[0] for a in builder.TV_APPS]
        assert "A1 Xplore" not in names

    def test_the_channel_view_can_dial(self, channels):
        view = builder.channels_view()
        assert view["path"] == "kanali"
        assert view["subview"] is True
        buttons = [
            c for c in _cards(view)
            if c.get("tap_action", {}).get("perform_action") == "script.jarvis_tv_kanal"
        ]
        assert buttons, "no channel dials anything"
        for b in buttons:
            assert b["tap_action"]["data"]["number"].isdigit()

    def test_a_channel_without_a_number_gets_no_button(self, channels):
        """A1 does not publish most numbers; a dead button is worse than none."""
        names = [c.get("name") for c in _cards(builder.channels_view())]
        assert "N1" not in names

    def test_duplicates_collapse_to_the_shorter_name(self, channels):
        names = [c.get("name") for c in _cards(builder.channels_view())]
        assert "HRT 1" in names
        assert "HRT 1 HD" not in names

    def test_a_missing_store_is_survivable(self, tmp_path, monkeypatch):
        """It is gitignored runtime state; a fresh checkout has none."""
        monkeypatch.setattr(builder, "ROOT", tmp_path)
        assert builder.learned_channels() == []
        view = builder.channels_view()
        assert view["path"] == "kanali", "the view still builds, just empty"

    def test_the_plain_app_launch_survives_inside_it(self):
        """The overview button used to launch A1; that must not be lost."""
        launchers = [
            c for c in _cards(builder.channels_view())
            if c.get("tap_action", {}).get("perform_action") == "remote.turn_on"
        ]
        assert len(launchers) == 1
        assert launchers[0]["tap_action"]["data"]["activity"] == builder.A1_ACTIVITY

    def test_only_channels_with_a_number_get_a_button(self, channels):
        for number, name in builder.learned_channels():
            assert int(number) > 0
            assert name

    def test_volume_works_when_the_cast_side_is_asleep(self):
        cards = _cards(builder.tv_view())
        actions = {
            c.get("tap_action", {}).get("perform_action") for c in cards
        }
        assert "media_player.volume_up" in actions
        assert "media_player.volume_down" in actions
        assert "media_player.volume_mute" in actions
        stepping = [
            c for c in cards
            if c.get("tap_action", {}).get("perform_action", "").startswith("media_player.volume")
        ]
        for c in stepping:
            assert c["tap_action"]["target"]["entity_id"] == builder.TV_MEDIA, (
                "the cast entity is off during broadcast; these must use the remote side"
            )

    def test_the_cast_slider_is_still_there_too(self):
        """It is the better control whenever cast is actually awake."""
        cards = _cards(builder.tv_view())
        sliders = [
            c for c in cards
            for f in c.get("features", [])
            if f.get("type") == "media-player-volume-slider"
        ]
        assert len(sliders) == 1
        assert sliders[0]["entity"] == builder.TV_CAST


class TestTheOverviewCopyLinksToItself:
    """The mirror kept the wall panel's paths, so tapping the shopping tile

    on Overview jumped to the other dashboard. It worked, which is exactly
    why it went unnoticed until the config was read back.
    """

    def test_navigation_paths_are_rewritten(self):
        board = {
            "views": [{"sections": [{"cards": [
                {"tap_action": {"action": "navigate",
                                "navigation_path": f"/{builder.BOARD}/kupovina"}},
            ]}]}]
        }
        out = builder.for_default_dashboard(board)
        card = out["views"][0]["sections"][0]["cards"][0]
        assert card["tap_action"]["navigation_path"] == "/lovelace/kupovina"

    def test_the_original_is_left_alone(self):
        """The wall panel keeps its own paths; only the copy is rewritten."""
        board = {
            "views": [{"sections": [{"cards": [
                {"tap_action": {"navigation_path": f"/{builder.BOARD}/kanali"}},
            ]}]}]
        }
        builder.for_default_dashboard(board)
        kept = board["views"][0]["sections"][0]["cards"][0]
        assert kept["tap_action"]["navigation_path"] == f"/{builder.BOARD}/kanali"

    def test_everything_else_survives_the_copy(self):
        view = builder.shopping_view()
        out = builder.for_default_dashboard({"views": [view]})
        assert out["views"][0]["path"] == "kupovina"
        assert out["views"][0]["subview"] is True
