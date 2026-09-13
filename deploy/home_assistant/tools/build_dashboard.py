"""Rebuild the Jarvis dashboard, laid out for the 7" wall panel.

Two things drive every choice here.

First, the panel is 1024x600 -- measured with grim on the panel itself, after
this file spent a long time assuming 800x480 and laying out for a screen that
was never there. Views are three columns wide, tiles are square and
finger-sized rather than full width rows, and prose is kept off the screen -- a
paragraph of explanation is unreadable at arm's length and steals room from the
controls. Height is the scarce axis: about 544px survives the header, roughly
eight grid rows, and a column taller than that has to be scrolled with a
fingertip. Hence three columns rather than two, and hence the lights living on
their own view instead of stacking sixteen tiles onto the overview.

Second, the room views are generated from the entity registry rather than
written by hand, so a room shows what is actually in it and a newly added
sensor appears by rerunning this. Generation needs an exclusion list, though:
the wall push-buttons behind each light, the two unconnected spare relays and
the node's own Wi-Fi and uptime readings are all real entities that nobody
wants to see while looking at a room.
"""

import asyncio
import itertools
import json
import os
import pathlib
import sys
from collections import defaultdict

import websockets

URL = os.environ["HA_URL"].replace("http://", "ws://") + "/api/websocket"
TOKEN = os.environ["HA_TOKEN"]
BOARD = "jarvis-dom"
# Repo root, for reading the agent's own stores (learned TV channels).
ROOT = pathlib.Path(__file__).resolve().parents[3]
# Assist pipeline the "Pitaj Jarvisa" buttons open. Empty = HA's preferred one.
PIPELINE = os.getenv("HA_ASSIST_PIPELINE_ID", "").strip()
_PIPELINE_ARG = {"pipeline_id": PIPELINE} if PIPELINE else {}
BACKUP = os.getenv("DASHBOARD_BACKUP", "/tmp/jarvis-dom-backup.json")
ENERGY_RESOURCE = "/local/jarvis-energy-card.js?v=20260913-2"

# Rooms in the order they should appear. "kuca" is deliberately absent: it is
# where house-wide things live, not a room, and it belongs on the System view.
ROOM_ORDER = [
    ("living_room", "mdi:sofa"),
    ("kitchen", "mdi:countertop"),
    ("blagavaona", "mdi:table-furniture"),
    ("bedroom", "mdi:bed"),
    ("kupaona", "mdi:shower"),
    ("hodnik", "mdi:door-open"),
    ("ulaz", "mdi:door"),
    ("terasa", "mdi:flower"),
    ("vani", "mdi:tree"),
]

# Entities that exist for good reasons but are noise inside a room. A room
# should answer "how is it in here", not list every channel of every sensor.
ROOM_EXCLUDE = (
    "tipkalo",              # the physical wall button behind each light
    "slobodno",             # spare relays with nothing wired to them
    "wifi_signal",          # belongs to the node, not the room
    "mux_uptime",
    "pokreni_ciscenje",     # SPS30 maintenance, lives on the Air view
    "broj_cestica",         # five particle counts; the Air view is their place
    "prosjecna_velicina",
    "kvaliteta_zraka_pm1",  # PM2.5 and PM10 are the two worth glancing at
    "kvaliteta_zraka_pm4",
)

CONTROL_DOMAINS = ("light", "switch", "media_player")

# One helper drives every graph on the board, so picking "Mjesec" on Klima
# leaves Zrak on the same span. Created in Home Assistant as an input_select;
# "Dan" is its first option and therefore the default after a restart.
PERIOD_HELPER = "input_select.razdoblje_grafova"

# What each choice means to a statistics graph. Day keeps the five-minute
# buckets, which is the finest Home Assistant stores and only for ten days;
# the longer spans coarsen so a year is twelve points per line rather than
# a hundred thousand.
PERIODS = (
    ("Dan", "5minute", 1),
    ("Tjedan", "hour", 7),
    ("Mjesec", "day", 31),
    ("Godina", "month", 365),
)


def period_picker(columns: int = 12, name: str = "Razdoblje") -> dict:
    return {
        "type": "tile", "entity": PERIOD_HELPER, "name": name,
        "icon": "mdi:calendar-range", "hide_state": True,
        "features_position": "inline",
        "features": [{"type": "select-options"}],
        "grid_options": {"columns": columns, "rows": 1},
    }


def period_toolbar(show_extremes: bool = False) -> dict:
    """One short, shared selector above the graphs, not inside one column."""
    cards = [period_picker(columns=12, name="Prikaz grafova")]
    if show_extremes:
        cards.append({"type": "button", "name": "Današnji min / max", "show_icon": False,
                      "grid_options": {"columns": 12, "rows": 1},
                      "tap_action": {"action": "navigate",
                                     "navigation_path": f"/{BOARD}/klima-rasponi"}})
    return {"type": "grid", "column_span": 3, "cards": cards}


def readings_list(entities: list[tuple[str, str]]) -> dict:
    """Native sensor rows keep full values, units and unavailable states."""
    return {"type": "entities", "show_header_toggle": False,
            "entities": [{"entity": entity, "name": name} for entity, name in entities],
            "grid_options": {"columns": 12, "rows": 4}}


def period_graphs(entities: list[tuple[str, str]], rows: int,
                  columns: int = 12) -> list[dict]:
    """The same series once per period, only one of them ever visible."""
    return [
        {
            "type": "conditional",
            "conditions": [{"condition": "or", "conditions": [
                {"condition": "state", "entity": PERIOD_HELPER, "state": state}
                for state in ((label, "unknown", "unavailable") if label == "Dan" else (label,))
            ]}],
            "card": {
                "type": "statistics-graph",
                "chart_type": "line",
                "period": period,
                "days_to_show": days,
                "stat_types": ["mean"],
                "entities": [{"entity": e, "name": n} for e, n in entities],
            },
            "grid_options": {"columns": columns, "rows": rows},
        }
        for label, period, days in PERIODS
    ]

TEMPERATURES = [
    ("sensor.bme280_mux_node_vanjska_temperatura", "Vani"),
    ("sensor.bme280_mux_node_dnevni_prostor_temperatura", "Boravak"),
    ("sensor.bme280_mux_node_soba_temperatura", "Soba"),
    ("sensor.bme280_mux_node_kupaona_temperatura", "Kupaona"),
    ("sensor.bme280_mux_node_ulaz_temperatura", "Ulaz"),
]

HUMIDITIES = [
    ("sensor.bme280_mux_node_vanjska_vlaga", "Vani"),
    ("sensor.bme280_mux_node_dnevni_prostor_vlaga", "Boravak"),
    ("sensor.bme280_mux_node_soba_vlaga", "Soba"),
    ("sensor.bme280_mux_node_kupaona_vlaga", "Kupaona"),
    ("sensor.bme280_mux_node_ulaz_vlaga", "Ulaz"),
]

PRESSURES = [
    ("sensor.bme280_mux_node_vanjski_tlak", "Vani"),
    ("sensor.bme280_mux_node_dnevni_prostor_tlak", "Boravak"),
    ("sensor.bme280_mux_node_soba_tlak", "Soba"),
    ("sensor.bme280_mux_node_kupaona_tlak", "Kupaona"),
    ("sensor.bme280_mux_node_ulaz_tlak", "Ulaz"),
]

# Power. Two clamps and a voltage transformer: one clamp on the incomer, one on
# the upstairs feed. The ground floor has no clamp of its own -- the node
# publishes it as the difference, which is honest for watts and only for watts
# (see the yaml). The three names here are the whole vocabulary of the power
# views, so a rename on the node is a one-line change in this file.
P_HOUSE = "sensor.bme280_mux_node_kuca_ukupno_stvarna_snaga"
P_UPSTAIRS = "sensor.bme280_mux_node_kat_stvarna_snaga"
P_GROUND = "sensor.bme280_mux_node_prizemlje_stvarna_snaga"
VOLTAGE = "sensor.bme280_mux_node_mrezni_napon"

POWER = [
    (P_HOUSE, "Kuća"),
    (P_UPSTAIRS, "Kat"),
    (P_GROUND, "Prizemlje"),
]

# Energy. House and upstairs are Riemann sums of their power sensors (HA
# integration helpers, created 2026-09-12); they count from zero at that moment
# and only ever rise, which is exactly what a meter reading is compared against.
# The ground floor is house minus upstairs (a template helper, 2026-09-13), not
# a sum of the ground-floor power: that power is a difference of two channels
# read ~1.3 s apart and used to swing negative, and its own integral had lost
# 0.02 kWh in half a day. The old helper, ..._potrosnja_prizemlje, still runs
# alongside as a cross-check.
E_HOUSE = "sensor.bme280_mux_node_potrosnja_kuca"
E_UPSTAIRS = "sensor.bme280_mux_node_potrosnja_kat"
E_GROUND = "sensor.bme280_mux_node_potrosnja_prizemlje_razlika"

ENERGY = [
    (E_HOUSE, "Kuća"),
    (E_UPSTAIRS, "Kat"),
    (E_GROUND, "Prizemlje"),
]

# The house meter, written down when the energy sensors started from zero.
# Everything the clamps have measured since adds to this, so the panel can show
# what the meter *should* read now -- the one number that settles whether the
# calibration is right, without a trip to the meter cupboard every time.
# Counters restarted from zero on 2026-09-13 00:15:46, straight after the
# channel-1 MUX fix (A0-A1 -> A0-A3). Everything counted before that is
# unusable: the upstairs channel read half, and the ground floor -- being the
# difference of the two -- read that much too high. The meter was read at 00:11
# (2315.8); the ~4.7 minutes until the counters started are folded in here so
# the comparison begins at the same instant as the meter.
METER_REF_KWH = 2315.85
METER_REF_WHEN = "13.9. 00:16"

def energy_card(mode: str = "summary", columns: int = 12) -> dict:
    """Read-only live readings; the module is served from HA's /local directory."""
    return {
        "type": "custom:jarvis-energy-card", "mode": mode,
        "entities": {
            "power_house": P_HOUSE, "power_upstairs": P_UPSTAIRS,
            "power_ground": P_GROUND, "energy_house": E_HOUSE,
            "energy_upstairs": E_UPSTAIRS, "energy_ground": E_GROUND,
            "voltage": VOLTAGE,
            "current": "sensor.bme280_mux_node_kuca_ukupno_struja",
            "factor": "sensor.bme280_mux_node_kuca_ukupno_faktor_snage",
        },
        "meter_reference": METER_REF_KWH, "meter_since": METER_REF_WHEN,
        "grid_options": {"columns": columns, "rows": "auto"},
    }


DEVICE_PREFIXES = ("ESP32 IO ", "BME280 Mux Node ", "Bme280 Mux Node ")

# A finger needs about a centimetre; a third of a 800 px view is about right.
TILE_COLUMNS = 4


def short_name(friendly: str, entity_id: str) -> str:
    name = friendly or entity_id.split(".", 1)[1].replace("_", " ")
    for prefix in DEVICE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name[:1].upper() + name[1:]


# Inside a room the only useful label for a reading is what it measures. The
# entity is called "Dnevni prostor temperatura"; the heading already said the
# room, so the card should just say "Temperatura".
MEASUREMENT_LABEL = (
    ("kvaliteta_zraka_pm2_5", "PM2.5"),
    ("kvaliteta_zraka_pm10", "PM10"),
    ("temperatura", "Temperatura"),
    ("vlaga", "Vlaga"),
    ("tlak", "Tlak"),
)


def reading_name(entity_id: str, fallback: str) -> str:
    for suffix, label in MEASUREMENT_LABEL:
        if entity_id.endswith(suffix):
            return label
    return fallback


def room_name(name: str, area_label: str) -> str:
    """Drop the room from the label -- the heading already says it."""
    trimmed = name
    for word in area_label.split():
        lowered = word.lower()
        if len(lowered) > 3 and lowered in trimmed.lower():
            start = trimmed.lower().index(lowered)
            trimmed = (trimmed[:start] + trimmed[start + len(lowered):]).strip(" -–")
    trimmed = " ".join(trimmed.split())
    return (trimmed[:1].upper() + trimmed[1:]) if trimmed else name


def tile(entity_id: str, name: str, toggle: bool, columns: int = TILE_COLUMNS,
         vertical: bool = True) -> dict:
    """One switch. Upright for thumbs, flat for lists.

    An upright tile stacks icon over name over state and costs about 105px; the
    flat one puts them on a line at 56. The lights and the appliances are grids
    you aim a thumb at and stay upright; a room is a list of a dozen things and
    lies flat, which is the difference between the Sobe view fitting the panel
    and not.
    """
    card = {
        "type": "tile",
        "entity": entity_id,
        "name": name,
        "vertical": vertical,
        "grid_options": {"columns": columns},
        "tap_action": {"action": "toggle"} if toggle else {"action": "more-info"},
    }
    if toggle:
        card["icon_tap_action"] = {"action": "toggle"}
    return card


def readings_table(entities: list[tuple[str, str]]) -> dict:
    """Names on one line, values on the next, no unit repeated five times."""
    names = " | ".join(name for _, name in entities)
    values = " | ".join(
        "{{ states('%s') | float(0) | round(0) }}" % entity_id
        for entity_id, _ in entities
    )
    return {
        "type": "markdown",
        "content": f"| {names} |\n" + "|:--:" * len(entities) + "|\n" + f"| {values} |\n",
        "grid_options": {"columns": 12, "rows": 2},
    }


def glance(entities: list[tuple[str, str]], title: str | None = None,
           columns: int = 3) -> dict:
    """A row of readings. `columns` is how many fit before it wraps.

    Five readings at three across is two rows with a gap in the second, which
    reads as an accident. Five across is one clean row and 90px shorter, and
    height is the scarce axis on this panel.
    """
    card = {
        "type": "glance",
        "columns": columns,
        "show_name": True,
        "show_state": True,
        "state_color": True,
        "entities": [{"entity": e, "name": n} for e, n in entities],
    }
    if title:
        card["title"] = title
    return card


# --- Overview ------------------------------------------------------------------

SUN_LINE = (
    "<font color='#e8a020'><ha-icon icon='mdi:weather-sunset-up'></ha-icon></font> "
    "{{ as_timestamp(states('sensor.sun_next_rising'), none) | timestamp_custom('%H:%M', default='—') }}"
    " &nbsp; <font color='#ed7946'><ha-icon icon='mdi:weather-sunset-down'></ha-icon></font> "
    "{{ as_timestamp(states('sensor.sun_next_setting'), none) | timestamp_custom('%H:%M', default='—') }}"
    "<br>"
    "{% set faze = {"
    "'new_moon': ['moon-new','Mladi Mjesec'],"
    "'waxing_crescent': ['moon-waxing-crescent','Srp u rastu'],"
    "'first_quarter': ['moon-first-quarter','Prva četvrt'],"
    "'waxing_gibbous': ['moon-waxing-gibbous','Mjesec raste'],"
    "'full_moon': ['moon-full','Puni Mjesec'],"
    "'waning_gibbous': ['moon-waning-gibbous','Mjesec opada'],"
    "'last_quarter': ['moon-last-quarter','Zadnja četvrt'],"
    "'waning_crescent': ['moon-waning-crescent','Srp u opadanju']} %}"
    "{% set f = faze.get(states('sensor.moon_phase'), ['help-circle-outline','Mijena nije dostupna']) %}"
    "<font color='#daa43b'><ha-icon icon='mdi:{{ f[0] }}'></ha-icon></font> **{{ f[1] }}**"
    "{% set uv = state_attr('weather.forecast_dom', 'uv_index') %}"
    "{% if is_number(uv) %} &nbsp; · &nbsp; UV {{ '%g' | format(uv | float | round(1)) }}{% endif %}"
    "<br>{% set day = is_state('sun.sun', 'above_horizon') %}"
    "{% set next = as_timestamp(states('sensor.sun_next_setting' if day else 'sensor.sun_next_rising'), none) %}"
    "{% if next is not none %}{% set seconds = [0, next - now().timestamp()] | max %}"
    "{{ 'Dan traje još' if day else 'Izlazak za' }} "
    "{{ (seconds // 3600) | int }} h {{ ((seconds % 3600) // 60) | int }} min"
    "{% endif %}"
)


def overview_view(all_light_ids: list[str],
                  favourites: list[tuple[str, str]] | None = None) -> dict:
    """The one view that is on screen all day.

    `favourites` are the lights switched most often over the last fortnight,
    from usage_order. They sit under the temperatures because that column ran
    out of content halfway down the panel, and because the light somebody
    reaches for is worth a thumb rather than two taps through the lights view.
    """
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "Pregled",
        "path": "pregled",
        "icon": "mdi:view-dashboard",
        "sections": [
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Svjetla", "heading_style": "title",
                     "icon": "mdi:lightbulb-group"},
                    {
                        "type": "button", "name": "Sva svjetla",
                        "icon": "mdi:lightbulb-group", "show_state": False,
                        "grid_options": {"columns": 6},
                        "tap_action": {"action": "navigate",
                                       "navigation_path": f"/{BOARD}/svjetla"},
                    },
                    {
                        "type": "button", "name": "Ugasi svjetla",
                        "icon": "mdi:lightbulb-off-outline", "show_state": False,
                        "grid_options": {"columns": 6},
                        "tap_action": {
                            "action": "perform-action",
                            "perform_action": "homeassistant.turn_off",
                            "target": {"entity_id": all_light_ids},
                        },
                    },
                    {"type": "heading", "heading": "Temperature", "heading_style": "title",
                     "icon": "mdi:thermometer"},
                    glance(TEMPERATURES, columns=5),
                    *([{"type": "heading", "heading": "Najčešće",
                        "heading_style": "title", "icon": "mdi:star-outline"}]
                      # Flat, not upright: on a 1366x768 laptop the upright
                      # pair of rows pushed this column past the fold, and a
                      # favourite is a line in a list, not a thumb target.
                      + [tile(entity_id, name, toggle=True, columns=6, vertical=False)
                         for entity_id, name in (favourites or [])[:4]]
                      if favourites else []),
                    energy_card("grid"),
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {
                        "type": "clock", "clock_style": "digital", "clock_size": "large",
                        "show_seconds": False, "no_background": True,
                        "time_zone": "Europe/Zagreb",
                        "grid_options": {"columns": 12, "rows": 2},
                    },
                    # "auto", not a fixed two rows: the line renders sunrise,
                    # sunset, the moon phase, UV and a countdown, and on the
                    # panel's narrow column that wraps past two rows. The moon
                    # was being clipped off the bottom, which read as the moon
                    # having disappeared (2026-09-06).
                    {"type": "markdown", "content": SUN_LINE,
                     "grid_options": {"columns": 12, "rows": "auto"}},
                    {
                        "type": "weather-forecast", "entity": "weather.forecast_dom",
                        "forecast_type": "daily", "show_current": True,
                        "show_forecast": True,
                        "grid_options": {"columns": 12, "rows": 4},
                    },
                    energy_card(),
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Kuća", "heading_style": "title",
                     "icon": "mdi:home-heart"},
                    # Three rows, not two: at two the needle and the right end
                    # of the scale are clipped by the card edge.
                    {"type": "gauge", "entity": "sensor.bme280_mux_node_kvaliteta_zraka_pm2_5",
                     "name": "PM2.5", "min": 0, "max": 250, "needle": True,
                     "grid_options": {"columns": 12, "rows": 3},
                     "severity": {"green": 0, "yellow": 35, "red": 100}},
                    {"type": "tile", "entity": "media_player.tv", "name": "TV",
                     "vertical": True, "grid_options": {"columns": 4}},
                    {"type": "tile", "entity": "person.tomislav", "name": "Tomislav",
                     "vertical": True, "grid_options": {"columns": 4}},
                    # A tile shows a todo entity's STATE, which is the number
                    # of open items -- useful at a glance, useless to shop
                    # from, and its more-info dialog opens the logbook. The
                    # count stays; the tap goes to a subview with the real list.
                    {"type": "tile", "entity": "todo.shopping_list", "name": "Kupovina",
                     "vertical": True, "grid_options": {"columns": 4},
                     "tap_action": {"action": "navigate",
                                    "navigation_path": f"/{BOARD}/kupovina"}},
                    {"type": "button", "name": "Pitaj Jarvisa", "icon": "mdi:microphone",
                     "show_state": False, "grid_options": {"columns": 12},
                     "tap_action": {"action": "assist", **_PIPELINE_ARG,
                                    "start_listening": True}},
                    {"type": "tile", "entity": "switch.jarvis_slusanje",
                     "name": "Mikrofon na Pi-ju", "icon": "mdi:microphone",
                     "grid_options": {"columns": 12}},
                ],
            },
        ],
    }


def lights_view(light_tiles: list[dict]) -> dict:
    """Every light in one grid, four across.

    Splitting them into three sections made three grids that each wrapped on
    their own, so the rows never lined up across the view and the last section
    ended short -- a ragged block with the right third of the screen empty. One
    section spanning all three columns is one grid: four tiles a row, every row
    the same, and the whole set visible without scrolling.

    The grid keeps 12 columns per section column, so a section spanning three
    is 36 wide -- hence tiles of 9 for a quarter each. At 3 they came out
    twelve to a row, thin enough to cut "blagavaona" in half.
    """
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "Svjetla",
        "path": "svjetla",
        "icon": "mdi:lightbulb-group",
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [{"type": "heading", "heading": "Svjetla",
                           "heading_style": "title", "icon": "mdi:lightbulb-group"}]
                         + light_tiles,
            }
        ],
    }


# --- Rooms ----------------------------------------------------------------------

def estimated_rows(cards: list[dict]) -> int:
    """Height of a card list in grid rows, packing the 12-column grid as HA does.

    Approximate by design: it only has to be good enough to balance columns.
    """
    default_rows = {"heading": 1, "tile": 1, "button": 2, "glance": 3, "gauge": 3,
                    "markdown": 3, "clock": 2, "weather-forecast": 5,
                    "history-graph": 5, "statistic": 2, "media-control": 4}
    full_width = {"heading", "glance", "history-graph", "markdown",
                  "weather-forecast", "media-control"}
    total = used = tallest = 0
    for card in cards:
        options = card.get("grid_options") or {}
        columns = options.get("columns") or (12 if card.get("type") in full_width else 4)
        rows = options.get("rows") or default_rows.get(card.get("type"), 2)
        if used + columns > 12:
            total += tallest
            used = tallest = 0
        used += columns
        tallest = max(tallest, rows)
    return total + tallest


def pack_into_columns(groups: list[list[dict]], columns: int) -> list[dict]:
    """Merge card groups into `columns` sections of roughly equal height.

    Sections lay out in rows and a row is as tall as its tallest section, so
    fewer, balanced sections beat many uneven ones: one section row means the
    view is exactly as tall as its fullest column and nothing lands below the
    fold. Groups keep their given order within a column, which matters because
    the first card of each is its room heading.
    """
    if len(groups) <= columns:
        return [{"type": "grid", "cards": cards} for cards in groups]

    # Contiguous split, not the cheapest bin packing. Balancing by height alone
    # would scatter the rooms and leave the panel reading kitchen, hallway,
    # bedroom in whatever order the arithmetic liked; ROOM_ORDER exists because
    # somebody decided how this house should be read. So: keep the order, and
    # pick the two cut points that give the shortest tallest column.
    sizes = [estimated_rows(cards) for cards in groups]
    best, best_cuts = None, None
    for cuts in _cut_points(len(groups), columns):
        bounds = (0, *cuts, len(groups))
        tallest = max(sum(sizes[a:b]) for a, b in zip(bounds, bounds[1:]))
        if best is None or tallest < best:
            best, best_cuts = tallest, bounds
    return [
        {"type": "grid", "cards": [card for cards in groups[a:b] for card in cards]}
        for a, b in zip(best_cuts, best_cuts[1:])
        if b > a
    ]


def _cut_points(count: int, columns: int):
    """Every way to cut `count` ordered groups into `columns` runs."""
    if columns <= 1:
        yield ()
        return
    for first in range(1, count - columns + 2):
        for rest in _cut_points(count - first, columns - 1):
            yield (first, *(first + r for r in rest))


def room_groups(by_area: dict, area_names: dict):
    """Stable room order; registry noise never becomes a room control."""
    for area_id, icon in ROOM_ORDER:
        entities = [e for e in by_area.get(area_id, [])
                    if wanted_in_room(e["entity_id"])]
        if entities:
            yield area_id, icon, area_names.get(area_id, area_id), entities


def rooms_view(by_area: dict, area_names: dict) -> dict:
    """Equal-size room cards. Controls live one tap away, not below the fold."""
    cards = []
    for area_id, icon, label, entities in room_groups(by_area, area_names):
        temperature = next((e["entity_id"] for e in entities
                            if e["entity_id"].endswith("temperatura")), None)
        humidity = next((e["entity_id"] for e in entities
                         if e["entity_id"].endswith("vlaga")), None)
        controls = [e["entity_id"] for e in entities
                    if e["entity_id"].split(".")[0] in ("light", "switch")]
        action = {"action": "navigate", "navigation_path": f"/{BOARD}/soba-{area_id}"}
        top = {"type": "tile", "entity": temperature or (controls[0] if controls else entities[0]["entity_id"]),
               "name": label, "icon": icon, "hide_state": temperature is None,
               "vertical": False,
               "tap_action": action, "icon_tap_action": action,
               "hold_action": {"action": "none"}}
        # JSON literals are also valid Jinja string/list literals here.
        status = ("{% set ids = " + json.dumps(controls) + " %}"
                  "{% set active = expand(ids) | selectattr('state','eq','on') | list | count %}"
                  "{% set missing = expand(ids) | selectattr('state','in',['unknown','unavailable']) | list | count %}"
                  "{{ active }} / {{ ids | count }} uključeno"
                  "{% if missing %} · {{ missing }} nedostupno{% endif %}") if controls else "Bez prekidača"
        if humidity:
            status += (" · {% set h = states('" + humidity + "') %}"
                       "{{ (h | float | round(0) | int) ~ ' %' if is_number(h) else '—' }} vlage")
        cards.append({"type": "vertical-stack", "grid_options": {"columns": 12, "rows": 2},
                      "cards": [top, {"type": "markdown", "content": status}]})
    # Three equal columns in row-major room order; no balancing by device count.
    return {"type": "sections", "max_columns": 3, "title": "Sobe", "path": "sobe",
            "icon": "mdi:floor-plan",
            "sections": [{"type": "grid", "column_span": 3, "cards": cards}] if cards else []}


def room_views(by_area: dict, area_names: dict) -> list[dict]:
    """Subview per room with a clear way back and all existing controls."""
    views = []
    for area_id, icon, label, entities in room_groups(by_area, area_names):
        sections = []
        for title, domains in (("Svjetla", ("light", "switch")),
                               ("Mediji", ("media_player",)),
                               ("Mjerenja", ("sensor", "binary_sensor"))):
            selected = [e for e in entities if e["entity_id"].split(".")[0] in domains]
            if not selected:
                continue
            cards = [{"type": "heading", "heading": "Svjetla i trošila" if title == "Svjetla" else title,
                      "heading_style": "title"}]
            if title == "Mjerenja":
                cards.append(readings_list([(e["entity_id"], reading_name(e["entity_id"], e["name"]))
                                            for e in selected]))
            else:
                for e in sorted(selected, key=lambda e: e["name"].casefold()):
                    cards.append(tile(e["entity_id"], room_name(e["name"], label),
                                      toggle=title == "Svjetla", columns=6, vertical=False))
            sections.append({"type": "grid", "cards": cards})
        views.append({"type": "sections", "max_columns": 3, "title": label, "icon": icon,
                      "path": f"soba-{area_id}", "subview": True,
                      "back_path": f"/{BOARD}/sobe", "sections": sections})
    return views


# --- Climate ---------------------------------------------------------------------

def climate_view() -> dict:
    """Aligned current readings and graphs, with a single shared period row."""
    return {
        "type": "sections", "max_columns": 3, "title": "Klima", "path": "klima",
        "icon": "mdi:thermometer",
        "sections": [period_toolbar(show_extremes=True)] + [
            {"type": "grid", "cards": [
                {"type": "heading", "heading": title, "heading_style": "title", "icon": icon},
                readings_list(entities),
                *period_graphs(entities, rows=4),
            ]}
            for title, icon, entities in (
                ("Temperatura", "mdi:thermometer", TEMPERATURES),
                ("Vlaga", "mdi:water-percent", HUMIDITIES),
                ("Tlak", "mdi:gauge", PRESSURES),
            )
        ],
    }


# --- Air ---------------------------------------------------------------------------


def climate_extremes_view() -> dict:
    """Keep daily minima/maxima available without stretching the main graphs."""
    return {
        "type": "sections", "max_columns": 3, "title": "Današnji min / max",
        "path": "klima-rasponi", "subview": True, "back_path": f"/{BOARD}/klima",
        "sections": [
            {"type": "grid", "cards": [
                {"type": "heading", "heading": name, "heading_style": "title"},
                *[{"type": "statistic", "entity": entity, "name": label,
                   "stat_type": stat, "period": {"calendar": {"period": "day"}},
                   "grid_options": {"columns": 6, "rows": 2}}
                  for stat, label in (("min", "Najniža"), ("max", "Najviša"))],
            ]}
            for entity, name in TEMPERATURES
        ],
    }

def air_view() -> dict:
    """The number now, the trend beside it, and the sensor's own knobs at the foot.

    The cleaning button and the average particle size used to hold a whole
    column of their own, which left the graph -- the only thing on this view
    worth looking at twice -- squeezed into a third of the width. They are
    settings, so they sit at the bottom of the readings column, and the graph
    takes the two columns they vacated.
    """
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "Zrak",
        "path": "zrak",
        "icon": "mdi:air-filter",
        "sections": [period_toolbar(),
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Kvaliteta zraka", "heading_style": "title",
                     "icon": "mdi:air-filter"},
                    {"type": "gauge", "entity": "sensor.bme280_mux_node_kvaliteta_zraka_pm2_5",
                     "name": "PM2.5", "min": 0, "max": 250, "needle": True,
                     "grid_options": {"columns": 12, "rows": 3},
                     "severity": {"green": 0, "yellow": 35, "red": 100}},
                    glance([
                        ("sensor.bme280_mux_node_kvaliteta_zraka_pm1", "PM1"),
                        ("sensor.bme280_mux_node_kvaliteta_zraka_pm4", "PM4"),
                        ("sensor.bme280_mux_node_kvaliteta_zraka_pm10", "PM10"),
                    ]),
                    {"type": "heading", "heading": "Senzor", "heading_style": "title",
                     "icon": "mdi:tune"},
                    {"type": "tile", "entity": "button.bme280_mux_node_sps30_pokreni_ciscenje",
                     "name": "Očisti senzor", "vertical": False,
                     "grid_options": {"columns": 12, "rows": 1}},
                    {"type": "tile", "entity": "sensor.bme280_mux_node_prosjecna_velicina_cestica",
                     "name": "Veličina čestica", "vertical": False,
                     "grid_options": {"columns": 12, "rows": 1}},
                ],
            },
            {
                "type": "grid",
                "column_span": 2,
                "cards": [
                    {"type": "heading", "heading": "Kroz vrijeme", "heading_style": "title",
                     "icon": "mdi:chart-line"},
                    # A section spanning two columns has a 24-wide grid, so
                    # 12 was half of it: the graph sat in its own left half
                    # with the picker beside it and the right half empty.
                    *period_graphs([
                        ("sensor.bme280_mux_node_kvaliteta_zraka_pm2_5", "PM2.5"),
                        ("sensor.bme280_mux_node_kvaliteta_zraka_pm10", "PM10"),
                    ], rows=8, columns=24),
                ],
            },
        ],
    }


# --- System -------------------------------------------------------------------------

def _status(entity: str, extra: str = "") -> str:
    """Whether the box is answering at all, as the first row of its own table.

    It used to be a `###` line above the table, which cost a heading's worth of
    space on every card -- four of them, and the System view was overflowing
    the panel by exactly about that much. No emoji either: the panel's font has
    no colour glyphs, so the emoji that used to start each device name came out
    as empty boxes on the wall. The name lives in a heading card now, which
    carries a real mdi icon.
    """
    return (
        "| Stanje | {% if states('" + entity + "') in "
        "['unavailable', 'unknown', 'none'] %}NE JAVLJA SE{% else %}Online{% endif %}"
        + extra + " |\n"
    )


def _uptime_from_boot(entity: str) -> str:
    return (
        "{% set b = states('" + entity + "') %}"
        "{% if b not in ['unavailable','unknown','none'] %}"
        "{% set s = (now().timestamp() - as_timestamp(b)) | int %}"
        "{{ s // 86400 }} d {{ (s % 86400) // 3600 }} h{% else %}—{% endif %}"
    )


def _uptime_from_seconds(entity: str) -> str:
    return (
        "{% set v = states('" + entity + "') %}"
        "{% if v not in ['unavailable','unknown','none'] %}"
        "{% set s = v | float(0) | int %}"
        "{{ s // 86400 }} d {{ (s % 86400) // 3600 }} h{% else %}—{% endif %}"
    )


HOME_ASSISTANT_CARD = (
    "| | |\n|---|--:|\n" + _status("sensor.system_monitor_processor_use",
              "{% if is_state('binary_sensor.rpi_power_status','on') %} · PODNAPON{% endif %}") +
    "| Procesor | {{ states('sensor.system_monitor_processor_use') }} % · "
    "{{ states('sensor.system_monitor_processor_temperature') }} °C |\n"
    "| Memorija | {{ states('sensor.system_monitor_memory_usage') }} % |\n"
    "| Disk | {{ states('sensor.system_monitor_disk_usage') }} % "
    "({{ states('sensor.system_monitor_disk_free_config') }} GiB) |\n"
    "| Radi | " + _uptime_from_boot("sensor.system_monitor_last_boot") + " |\n"
    "| IP | {{ states('sensor.system_monitor_ipv4_address_end0') }} |\n"
    "| Verzija | {{ state_attr('update.home_assistant_core_update','installed_version') }}"
    "{% if is_state('update.home_assistant_core_update','on') %} \u2192 "
    "{{ state_attr('update.home_assistant_core_update','latest_version') }}{% endif %} |\n"
    ""
)

JARVIS_CARD = (
    "| | |\n|---|--:|\n" + _status("sensor.jarvis_pi_cpu",
              "{% if states('stt.jarvis_stt') not in ['unavailable','unknown'] %}"
              " · sluša i govori{% else %} · GLAS NEDOSTUPAN{% endif %}") +
    "| Procesor | {{ states('sensor.jarvis_pi_cpu') }} % · "
    "{{ states('sensor.jarvis_pi_temperature') }} °C |\n"
    "| Memorija | {{ states('sensor.jarvis_pi_memory') }} % |\n"
    "| Disk | {{ states('sensor.jarvis_pi_disk') }} % "
    "({{ states('sensor.jarvis_pi_disk_free_gb') }} GB) |\n"
    "| Radi | " + _uptime_from_boot("sensor.jarvis_pi_boot") + " |\n"
    "| IP | {{ states('sensor.jarvis_pi_ip') }} |\n"
    ""
)

# The ESP32 knows its own address, how long it has been up, how well it hears
# the access point and how warm it is. None of that reached the panel, so the
# node that switches every light in the house was the one box you could not
# actually check on.
ESP32_CARD = (
    "| | |\n|---|--:|\n" + _status("switch.esp32_io_svjetlo_kuhinja") +
    "| IP | {{ states('sensor.esp32_io_ip') }} |\n"
    "| Radi | " + _uptime_from_seconds("sensor.esp32_io_uptime") + " |\n"
    "| Wi-Fi | {{ states('sensor.esp32_io_wifi_signal') | float(0) | round(0) }} dBm · "
    "\u010dip {{ states('sensor.esp32_io_temperatura_cipa') | float(0) | round(1) }} °C |\n"
    "| Firmware | {{ state_attr('update.esp32_io_firmware','installed_version') or '—' }} |\n"
    "| Svjetla | {{ states.switch | selectattr('entity_id','search','esp32_io_svjetlo') "
    "| selectattr('state','eq','on') | list | count }} upaljenih |\n"
    "| Uti\u010dnice | {{ states.switch | selectattr('entity_id','search','esp32_io_uticnica') "
    "| selectattr('state','eq','on') | list | count }} uklju\u010denih |\n"
)

BME_CARD = (
    "| | |\n|---|--:|\n" + _status("sensor.bme280_mux_node_bme280_mux_uptime") +
    "| IP | {{ states('sensor.bme280_mux_node_bme280_mux_ip') }} |\n"
    "| Radi | " + _uptime_from_seconds("sensor.bme280_mux_node_bme280_mux_uptime") + " |\n"
    "| Wi-Fi | {{ states('sensor.bme280_mux_node_bme280_mux_wifi_signal') "
    "| float(0) | round(0) }} dBm · \u010dip "
    "{{ states('sensor.bme280_mux_node_bme280_mux_temperatura_cipa') "
    "| float(0) | round(1) }} °C |\n"
    # The node's own chip temperature matches the room-sensor pattern, so the
    # tally read 6 of 5 -- a number that can only be wrong. Rooms only.
    "| Senzori | {{ states.sensor "
    "| selectattr('entity_id','search','bme280_mux_node_.*temperatura') "
    "| rejectattr('entity_id','search','cipa') "
    "| rejectattr('state','in',['unavailable','unknown']) | list | count }} / 5 |\n"
    "| PM2.5 | {{ states('sensor.bme280_mux_node_kvaliteta_zraka_pm2_5') "
    "| float(0) | round(1) }} µg/m³ |\n"
    "| Napon | {% set v = states('sensor.bme280_mux_node_mrezni_napon') %}"
    "{% if v in ['unavailable','unknown','none'] %}— (mjerenje ne radi)"
    "{% else %}{{ v | float(0) | round(1) }} V{% endif %} |\n"
)


def _box(title: str, icon: str, content: str) -> list[dict]:
    """A named box: a heading card for the name, markdown for the numbers."""
    return [
        {"type": "heading", "heading": title, "heading_style": "title", "icon": icon},
        {"type": "markdown", "content": content},
    ]


def power_view() -> dict:
    """One row of sections: readings beside a wide graph and meter reference."""
    return {
        "type": "sections", "max_columns": 3,
        "title": "Energija", "path": "potrosnja", "icon": "mdi:lightning-bolt",
        "sections": [
            {"type": "grid", "cards": [energy_card("detail"), energy_card("totals")]},
            {
                "type": "grid", "column_span": 2,
                "cards": [
                    period_picker(columns=24, name="Kretanje snage"),
                    *period_graphs(POWER, rows=7, columns=24),
                    energy_card("meter", columns=24),
                ],
            },
        ],
    }


def system_view() -> dict:
    """The two machines, the two nodes, and the lists -- one column each.

    Six sections in a three-wide view wrapped onto a second section row, and a
    section row starts below the tallest card of the one above it: the update
    and add-on lists began half off the bottom of the panel. Three named
    columns keep the whole view to a single row.
    """
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "Sustav",
        "path": "sustav",
        "icon": "mdi:heart-pulse",
        "sections": [
            {
                "type": "grid",
                "cards": _box("Home Assistant", "mdi:home-assistant", HOME_ASSISTANT_CARD)
                + _box("Jarvis", "mdi:robot", JARVIS_CARD),
            },
            {
                "type": "grid",
                "cards": _box("ESP32 I/O", "mdi:chip", ESP32_CARD)
                + _box("BME280 Mux Node", "mdi:thermometer-lines", BME_CARD),
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Nadogradnje", "heading_style": "title",
                     "icon": "mdi:package-up"},
                    {"type": "entities", "entities": [
                        {"entity": "update.home_assistant_core_update", "name": "Home Assistant"},
                        {"entity": "update.home_assistant_operating_system_update", "name": "OS"},
                        {"entity": "update.home_assistant_supervisor_update", "name": "Supervisor"},
                        {"entity": "update.esphome_device_builder_update", "name": "ESPHome"},
                        {"entity": "update.mosquitto_broker_update", "name": "Mosquitto"},
                        {"entity": "update.tailscale_update", "name": "Tailscale"},
                    ]},
                    {"type": "heading", "heading": "Dodaci", "heading_style": "title",
                     "icon": "mdi:puzzle"},
                    {"type": "entities", "entities": [
                        {"entity": "binary_sensor.mosquitto_broker_running", "name": "Mosquitto"},
                        {"entity": "binary_sensor.tailscale_running", "name": "Tailscale"},
                        {"entity": "binary_sensor.esphome_device_builder_running", "name": "ESPHome"},
                        {"entity": "binary_sensor.studio_code_server_running", "name": "Studio Code"},
                        {"entity": "binary_sensor.samba_share_running", "name": "Samba"},
                    ]},
                ],
            },
        ],
    }


# --- Lights and sockets --------------------------------------------------------------

def appliances_view(sockets: list[tuple[str, str]]) -> dict:
    """The sockets, in the same grid as the lights.

    They used to share a view with the lights, which listed all sixteen of
    those a second time; whichever tab you opened, half of it was the other
    one. Same layout as Svjetla, one section across the full width, four to a
    row, ordered by what actually gets switched.
    """
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "Trošila",
        "path": "trosila",
        "icon": "mdi:power-socket-eu",
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [{"type": "heading", "heading": "Trošila",
                           "heading_style": "title", "icon": "mdi:power-socket-eu"}]
                         + [tile(entity_id, name, toggle=True, columns=9)
                            for entity_id, name in sockets],
            }
        ],
    }


# --- TV ----------------------------------------------------------------------------------

# What the TV is called on each of the two protocols it speaks. The remote can
# press keys and launch apps; the cast side is the only one that accepts an
# exact volume. Both are used for what each is good at, and neither is
# explained on screen -- a paragraph about protocols is not something anyone
# reads while reaching for the volume.
TV_REMOTE = "remote.tv"
# The androidtv_remote side: always awake while the TV is on, and the only
# one that reports which app is in front.
TV_MEDIA = "media_player.tv"
TV_PLAYER = "media_player.tv"
TV_CAST = "media_player.smart_tv_pro"

# How an app is opened matters more than which app it is. A deep link is sent
# as it is; a bare package name does nothing at all on this television, so it
# goes through the small launcher app installed on it -- the same route Jarvis
# uses, and the reason that app exists. Buttons built on raw package names
# would look right and do nothing.
# A1 is deliberately not here: its button opens the channel list instead of
# the app, because switching to a channel is what anyone actually wants from
# it. The plain app launch lives at the top of that list.
A1_ACTIVITY = "jarvis://open?pkg=hr.a1.android.tv.xploretv"
A1_PACKAGE = "hr.a1.android.tv.xploretv"

TV_APPS = [
    ("YouTube", "mdi:youtube", "https://www.youtube.com"),
    ("Netflix", "mdi:netflix", "https://www.netflix.com/title"),
]


# The remote entity reports a raw package name; nobody wants to read
# com.google.android.youtube.tv on a wall. Anything unmapped still shows, so a
# newly installed app is visible rather than swallowed.
TV_NOW = (
    "{% set app = state_attr('media_player.tv','app_id') %}"
    "{% set imena = {"
    "'com.google.android.youtube.tv': 'YouTube',"
    "'hr.a1.android.tv.xploretv': 'A1 Xplore TV',"
    "'com.netflix.ninja': 'Netflix',"
    "'com.google.android.apps.tv.launcherx': 'početni zaslon',"
    "'com.google.android.apps.tv.dreamx': 'čuvar zaslona'} %}"
    "{% if is_state('media_player.tv','off') %}Televizor je ugašen."
    "{% else %}Uključen &nbsp;·&nbsp; "
    "{{ imena.get(app, app if app else 'nepoznata aplikacija') }}{% endif %}"
)


def key(name: str, icon: str, command: str, columns: int = 4) -> dict:
    """One remote key. Verified against the TV: send_command moves it."""
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "show_state": False,
        "grid_options": {"columns": columns},
        "tap_action": {
            "action": "perform-action",
            "perform_action": "remote.send_command",
            "target": {"entity_id": TV_REMOTE},
            "data": {"command": command},
        },
    }


def app_button(name: str, icon: str, package: str) -> dict:
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "show_state": False,
        "grid_options": {"columns": 4},
        "tap_action": {
            "action": "perform-action",
            "perform_action": "remote.turn_on",
            "target": {"entity_id": TV_REMOTE},
            "data": {"activity": package},
        },
    }


DEFAULT_BOARD = "lovelace"


def for_default_dashboard(config: dict) -> dict:
    """The same board, with its internal links pointing at itself.

    Navigation paths carry the dashboard they belong to, so a copy saved to
    Overview would send anyone tapping the shopping tile over to the wall
    panel dashboard instead of staying where they are. It still works, which
    is what makes it easy to miss.
    """
    def rewrite(node):
        if isinstance(node, dict):
            return {
                k: (v.replace(f"/{BOARD}/", f"/{DEFAULT_BOARD}/", 1)
                    if k == "navigation_path" and isinstance(v, str)
                    else rewrite(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [rewrite(v) for v in node]
        return node

    return rewrite(config)


def shopping_view() -> dict:
    """The shopping list itself, reached by tapping the count on the overview."""
    return {
        "type": "sections",
        "title": "Lista za kupovinu",
        "path": "kupovina",
        "icon": "mdi:cart-outline",
        "subview": True,
        "max_columns": 2,
        "sections": [{
            "type": "grid",
            "cards": [{
                "type": "todo-list",
                "entity": "todo.shopping_list",
                "title": "Lista za kupovinu",
                "display_order": "none",
            }],
        }],
    }


def learned_channels() -> list[tuple[int, str]]:
    """(number, shortest name) for every channel Jarvis knows a number for.

    Read from the agent's own store, so a channel learned by saying "N1 je 105"
    appears on the panel the next time this runs. Most entries came from the
    operator's published list and have no number -- A1 does not publish them --
    and a button that cannot dial anything is worse than no button.

    Shortest name wins among duplicates: "HRT 1" and "HRT 1 HD" are one channel,
    and the short one fits the button.
    """
    store = ROOT / "config" / "tv_channels.json"
    try:
        channels = json.loads(store.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  upozorenje: ne mogu procitati {store}: {exc}")
        return []

    best: dict[int, str] = {}
    for name, data in channels.items():
        number = (data or {}).get("number")
        if not number:
            continue
        number = int(number)
        if number not in best or len(name) < len(best[number]):
            best[number] = name
    return sorted(best.items())


def channels_view() -> dict:
    """Programmed channels, one button each.

    The button calls script.jarvis_tv_kanal, which mirrors the tv_channel tool:
    launch the app when it is not already in front, wait out the warm-up, step
    into live TV, then type the digits. The waits are long because the app is
    slow to draw and digits sent early are simply lost.
    """
    channels = learned_channels()
    cards = [
        {"type": "heading", "heading": "A1 Xplore TV — programirani kanali",
         "heading_style": "title", "icon": "mdi:television-guide"},
        {"type": "button", "name": "Otvori A1 Xplore", "icon": "mdi:open-in-app",
         "show_state": False, "grid_options": {"columns": 12},
         "tap_action": {"action": "perform-action",
                        "perform_action": "remote.turn_on",
                        "target": {"entity_id": TV_REMOTE},
                        "data": {"activity": A1_ACTIVITY}}},
    ]
    cards += [
        {"type": "button", "name": name, "icon": "mdi:television-classic",
         "show_state": False, "grid_options": {"columns": 6},
         "tap_action": {"action": "perform-action",
                        "perform_action": "script.jarvis_tv_kanal",
                        "data": {"number": str(number)}}}
        for number, name in channels
    ]
    print(f"  kanala s brojem: {len(channels)}")
    return {
        "type": "sections",
        "title": "Kanali",
        "path": "kanali",
        "icon": "mdi:television-guide",
        "subview": True,
        "max_columns": 3,
        "sections": [{"type": "grid", "cards": cards}],
    }


def volume_keys() -> list[dict]:
    """Step volume on the remote entity, for when the cast slider is asleep."""
    def key_card(name: str, icon: str, action: str, data: dict | None = None) -> dict:
        card = {"type": "button", "name": name, "icon": icon, "show_state": False,
                "grid_options": {"columns": 4},
                "tap_action": {"action": "perform-action",
                               "perform_action": action,
                               "target": {"entity_id": TV_MEDIA}}}
        if data:
            card["tap_action"]["data"] = data
        return card

    return [
        key_card("Tiše", "mdi:volume-minus", "media_player.volume_down"),
        key_card("Mute", "mdi:volume-off", "media_player.volume_mute",
                 {"is_volume_muted": True}),
        key_card("Glasnije", "mdi:volume-plus", "media_player.volume_up"),
    ]


def tv_view() -> dict:
    groups = [
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Sada", "heading_style": "title",
                     "icon": "mdi:television-play"},
                    # The cast side is the one that knows what is playing:
                    # title, artist, artwork, how far in. The remote side knows
                    # only a package name -- measured, with Elvis on screen:
                    # cast said "Elvis Presley - '68 Comeback Special", the
                    # remote said "com.google.android.youtube.tv". It also
                    # takes an absolute volume, so its slider lands where you
                    # put it instead of stepping towards it.
                    {"type": "media-control", "entity": TV_CAST},
                    {"type": "tile", "entity": TV_CAST, "name": "Glasnoća",
                     "features": [{"type": "media-player-volume-slider"}]},
                    # The slider above belongs to the cast side, which is off
                    # whenever the TV is showing a broadcast channel -- so it
                    # sits there dead through most of an evening. The remote
                    # side is always awake but has no absolute volume, only
                    # steps, which is why these are buttons and not a second
                    # slider. Mute only mutes: the service takes an explicit
                    # value and a dashboard button cannot compute a toggle.
                    *volume_keys(),
                    # Cast falls silent on broadcast channels, so this line is
                    # what still says the set is on and what it is showing.
                    {"type": "markdown", "content": TV_NOW},
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Daljinski", "heading_style": "title",
                     "icon": "mdi:remote-tv"},
                    key("Natrag", "mdi:arrow-u-left-top", "BACK"),
                    key("Gore", "mdi:chevron-up", "DPAD_UP"),
                    key("Početna", "mdi:home", "HOME"),
                    key("Lijevo", "mdi:chevron-left", "DPAD_LEFT"),
                    key("OK", "mdi:circle-slice-8", "DPAD_CENTER"),
                    key("Desno", "mdi:chevron-right", "DPAD_RIGHT"),
                    key("Izbornik", "mdi:dots-horizontal", "MENU"),
                    key("Dolje", "mdi:chevron-down", "DPAD_DOWN"),
                    key("Pauza", "mdi:play-pause", "MEDIA_PLAY_PAUSE"),
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Glasnoća i kanali",
                     "heading_style": "title", "icon": "mdi:volume-high"},
                    key("Tiše", "mdi:volume-minus", "VOLUME_DOWN"),
                    key("Bez zvuka", "mdi:volume-off", "VOLUME_MUTE"),
                    key("Glasnije", "mdi:volume-plus", "VOLUME_UP"),
                    key("Kanal −", "mdi:chevron-double-down", "CHANNEL_DOWN", columns=6),
                    key("Kanal +", "mdi:chevron-double-up", "CHANNEL_UP", columns=6),
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Aplikacije", "heading_style": "title",
                     "icon": "mdi:apps"},
                    *[app_button(*app) for app in TV_APPS],
                    # A1 opens the channel list rather than the app: what
                    # anyone wants from it is a channel, and the app is one
                    # tap further in. Without this nothing links to that view.
                    {"type": "button", "name": "A1 Xplore",
                     "icon": "mdi:television-classic", "show_state": False,
                     "grid_options": {"columns": 4},
                     "tap_action": {"action": "navigate",
                                    "navigation_path": f"/{BOARD}/kanali"}},
                ],
            },
            {
                "type": "grid",
                "cards": [
                    {"type": "heading", "heading": "Napajanje", "heading_style": "title",
                     "icon": "mdi:power-plug"},
                    {"type": "tile", "entity": TV_PLAYER, "name": "Televizor",
                     "vertical": True, "grid_options": {"columns": 4},
                     "tap_action": {"action": "toggle"}},
                    {"type": "tile", "entity": "switch.esp32_io_uticnica_tv",
                     "name": "Utičnica", "vertical": True,
                     "grid_options": {"columns": 4}, "tap_action": {"action": "toggle"}},
                    {"type": "tile", "entity": "switch.esp32_io_svjetlo_tv",
                     "name": "Svjetlo", "vertical": True,
                     "grid_options": {"columns": 4}, "tap_action": {"action": "toggle"}},
                ],
            },
    ]
    return {
        "type": "sections",
        "max_columns": 3,
        "title": "TV",
        "path": "tv",
        "icon": "mdi:television",
        # Packed automatically this put Glasnoća, Aplikacije *and* Napajanje
        # in the third column -- the power tiles sat at the very bottom edge
        # while the first two columns had a third of their height spare. The
        # estimator underrates the media card, so the columns are named here:
        # what is playing and the plugs on the left, the pad in the middle,
        # volume and apps on the right.
        "sections": [
            {"type": "grid", "cards": groups[0]["cards"] + groups[4]["cards"]},
            {"type": "grid", "cards": groups[1]["cards"]},
            {"type": "grid", "cards": groups[2]["cards"] + groups[3]["cards"]},
        ],
    }


# --- Conversation ----------------------------------------------------------------------

RAZGOVOR = (
    "{% set turns = state_attr('sensor.jarvis_razgovor', 'povijest') %}"
    "{% if turns %}{% for t in turns %}"
    "**{{ as_timestamp(t['vrijeme']) | timestamp_custom('%d.%m. %H:%M') }} · ti**\n"
    "{{ t['pitanje'] }}\n\n**Jarvis**\n{{ t['odgovor'] }}\n\n---\n"
    "{% endfor %}{% else %}_Još nema zapisanog razgovora._{% endif %}"
)


def conversation_view() -> dict:
    return {
        "type": "sections", "max_columns": 1, "title": "Razgovor", "path": "razgovor",
        "icon": "mdi:message-text-clock",
        "sections": [{
            "type": "grid",
            "cards": [
                {"type": "heading", "heading": "Razgovor s Jarvisom",
                 "heading_style": "title", "icon": "mdi:robot"},
                {"type": "button", "name": "Pitaj Jarvisa", "icon": "mdi:microphone",
                 "show_state": False, "grid_options": {"columns": 12},
                 "tap_action": {"action": "assist", **_PIPELINE_ARG,
                                "start_listening": True}},
                {"type": "tile", "entity": "switch.jarvis_slusanje",
                 "name": "Mikrofon na Pi-ju", "icon": "mdi:microphone",
                 "grid_options": {"columns": 12}},
                {"type": "markdown", "content": RAZGOVOR},
            ],
        }],
    }


# --- wiring ------------------------------------------------------------------------------

# Home Assistant rejects a websocket message whose id is not higher than the
# last one it saw ("id_reuse"). Hand-numbered ids meant that inserting a call
# near the top of main() silently broke every call after it, which is exactly
# what happened when the resource registration was added ahead of the existing
# 1, 2, 3. A counter removes the whole class of bug: ask for the next id at the
# call site and the order takes care of itself.
_IDS = itertools.count(1)


def next_id() -> int:
    return next(_IDS)


async def call(ws, ident, payload):
    await ws.send(json.dumps({"id": ident, **payload}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == ident and msg.get("type") == "result":
            if not msg.get("success"):
                sys.exit("GRESKA: " + json.dumps(msg.get("error"), ensure_ascii=False))
            return msg.get("result")


def wanted_in_room(entity_id: str) -> bool:
    return not any(word in entity_id for word in ROOM_EXCLUDE)


def count_switch_ons(history: dict, entity_ids: list[str]) -> dict[str, int]:
    """How many times each switch was actually turned on, from recorded history.

    Only off -> on counts. A node reboot republishes every entity, and counting
    unavailable -> on made all eleven sockets look equally busy at thirteen
    switches each -- an artefact, not a habit.
    """
    counts = {}
    for entity_id in entity_ids:
        previous = None
        total = 0
        for item in history.get(entity_id) or []:
            state = item.get("s", item.get("state"))
            if state == "on" and previous == "off":
                total += 1
            previous = state
        counts[entity_id] = total
    return counts


async def usage_order(ws, ident: int, entity_ids: list[str], days: int = 14) -> dict[str, int]:
    """Recent switch counts, or an empty map if the recorder cannot answer."""
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    try:
        history = await call(ws, ident, {
            "type": "history/history_during_period",
            "start_time": (end - timedelta(days=days)).isoformat(),
            "end_time": end.isoformat(),
            "entity_ids": entity_ids,
            "minimal_response": True,
            "no_attributes": True,
        })
    except SystemExit:
        print("  (povijest nedostupna — poredak ostaje abecedni)")
        return {}
    return count_switch_ons(history, entity_ids)


async def main():
    async with websockets.connect(URL, open_timeout=10, max_size=40_000_000) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth odbijen")

        # Copy www/jarvis-energy-card.js to HA /config/www before generating.
        resources = await call(ws, next_id(), {"type": "lovelace/resources"})
        resource = next((r for r in resources
                         if r.get("url", "").split("?")[0] == ENERGY_RESOURCE.split("?")[0]), None)
        if resource is None:
            await call(ws, next_id(), {"type": "lovelace/resources/create",
                                "res_type": "module", "url": ENERGY_RESOURCE})
        elif resource.get("url") != ENERGY_RESOURCE:
            await call(ws, next_id(), {"type": "lovelace/resources/update",
                                "resource_id": resource["id"],
                                "res_type": "module", "url": ENERGY_RESOURCE})

        areas = await call(ws, next_id(), {"type": "config/area_registry/list"})
        entities = await call(ws, next_id(), {"type": "config/entity_registry/list"})
        states = {s["entity_id"]: s for s in await call(ws, next_id(), {"type": "get_states"})}
        area_names = {a["area_id"]: a["name"] for a in areas}

        by_area = defaultdict(list)
        for entry in entities:
            area_id = entry.get("area_id")
            entity_id = entry["entity_id"]
            if not area_id or entry.get("disabled_by") or entry.get("hidden_by"):
                continue
            if entity_id not in states or not wanted_in_room(entity_id):
                continue
            by_area[area_id].append({
                "entity_id": entity_id,
                "name": short_name(states[entity_id]["attributes"].get("friendly_name", ""),
                                   entity_id),
            })

        def label(entity_id: str) -> str:
            name = short_name(states[entity_id]["attributes"].get("friendly_name", ""), entity_id)
            return name.replace("Svjetlo ", "").replace("Utičnica ", "")

        # Everything switchable that is not disabled or hidden. Split by what it
        # IS, not by what it is called: listing sockets by the word "utičnica"
        # silently lost the oven, which is a switch by any other name.
        switchable = [
            entry["entity_id"]
            for entry in entities
            if entry["entity_id"].split(".")[0] in ("switch", "light")
            and not entry.get("disabled_by")
            and not entry.get("hidden_by")
            and entry["entity_id"] in states
        ]
        # Order by what actually gets touched, so the thumb lands on the bathroom
        # light rather than on whatever starts with B.
        used = await usage_order(ws, next_id(), switchable)

        def by_use(entity_id: str) -> tuple:
            return (-used.get(entity_id, 0), label(entity_id).lower())

        lights = [(e, label(e)) for e in sorted(
            (e for e in switchable if "svjetlo" in e), key=by_use)]
        sockets = [(e, label(e)) for e in sorted(
            (e for e in switchable
             if "svjetlo" not in e and e != "switch.jarvis_slusanje"), key=by_use)]
        if used:
            top = ", ".join(f"{label(e)} ({used.get(e, 0)}×)" for e, _ in lights[:4])
            print(f"  najčešće paljena svjetla u 14 dana: {top}")
        light_ids = [entity_id for entity_id, _ in lights]

        print(f"  svjetala: {len(lights)}, utičnica: {len(sockets)}")
        for area_id, _ in ROOM_ORDER:
            print(f"    {area_names.get(area_id, area_id):16} {len(by_area.get(area_id) or [])}")

        cfg = await call(ws, next_id(), {"type": "lovelace/config", "url_path": BOARD})
        with open(BACKUP, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        print(f"  kopija prije izmjene: {BACKUP}")

        cfg["views"] = [
            overview_view(light_ids, lights[:4]),
            lights_view([tile(e, n, True, columns=9) for e, n in lights]),
            appliances_view(sockets),
            rooms_view(by_area, area_names),
            climate_view(),
            air_view(),
            power_view(),
            tv_view(),
            system_view(),
            conversation_view(),
            climate_extremes_view(),
            shopping_view(),
            channels_view(),
            *room_views(by_area, area_names),
        ]

        await call(ws, next_id(), {"type": "lovelace/config/save", "url_path": BOARD, "config": cfg})
        after = await call(ws, next_id(), {"type": "lovelace/config", "url_path": BOARD})
        print("  prikazi:", [v.get("title") for v in after["views"]])

        # The same board is written to Overview as well. Opening Home Assistant
        # from a phone or a laptop lands on the default dashboard, and the
        # per-user defaultPanel setting is overridden by whatever a browser has
        # stored locally as "default on this device" -- so the reliable way to
        # arrive at the wall panel is for Overview to BE the wall panel. Set
        # MIRROR_TO_OVERVIEW=false to keep them separate.
        if os.getenv("MIRROR_TO_OVERVIEW", "true").lower() != "false":
            mirror = await call(ws, next_id(), {"type": "lovelace/config"})
            with open(BACKUP + ".overview", "w", encoding="utf-8") as fh:
                json.dump(mirror, fh, ensure_ascii=False, indent=2)
            await call(ws, next_id(), {"type": "lovelace/config/save",
                                "config": for_default_dashboard(cfg)})
            print("  Overview preslikan na zidni panel")


if __name__ == "__main__":  # importable, so the pure helpers can be tested
    asyncio.run(main())
