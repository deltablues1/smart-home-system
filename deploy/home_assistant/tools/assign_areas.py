"""Registry hygiene for the Home Assistant instance: rooms, diagnostics, duplicates.

Three jobs, all of which have to be repeatable because Home Assistant recreates
entities whenever an integration is reloaded:

* put entities in the room they are physically in, since everything hangs off
  two ESPHome devices and would otherwise inherit those devices' areas,
* switch on the diagnostics Home Assistant ships disabled and the System view
  needs,
* silence duplicates. The ESP32 node publishes over both the ESPHome native API
  and MQTT, so once the MQTT integration was added Home Assistant created a
  second copy of every switch, suffixed `_2`. Both work; showing both is
  confusing. The native API copy is kept because it is the one already assigned
  to rooms and used by everything else.
"""

import asyncio
import json
import os
import sys

import websockets

URL = os.environ["HA_URL"].replace("http://", "ws://") + "/api/websocket"
TOKEN = os.environ["HA_TOKEN"]
BACKUP = os.getenv("REGISTRY_BACKUP", "/tmp/entity-registry-backup.json")

# suffix of the entity_id -> area_id
ROOM_BY_SUFFIX = {
    # --- BME280 mux node: climate per room ---
    "dnevni_prostor_temperatura": "living_room",
    "dnevni_prostor_vlaga": "living_room",
    "dnevni_prostor_tlak": "living_room",
    "kupaona_temperatura": "kupaona",
    "kupaona_vlaga": "kupaona",
    "kupaona_tlak": "kupaona",
    "soba_temperatura": "bedroom",
    "soba_vlaga": "bedroom",
    "soba_tlak": "bedroom",
    "ulaz_temperatura": "ulaz",
    "ulaz_vlaga": "ulaz",
    "ulaz_tlak": "ulaz",
    "vanjska_temperatura": "vani",
    "vanjska_vlaga": "vani",
    "vanjski_tlak": "vani",
    # --- air quality: the SPS30 sits in the living area ---
    "kvaliteta_zraka_pm1": "living_room",
    "kvaliteta_zraka_pm2_5": "living_room",
    "kvaliteta_zraka_pm4": "living_room",
    "kvaliteta_zraka_pm10": "living_room",
    "broj_cestica_pm0_5": "living_room",
    "broj_cestica_pm1": "living_room",
    "broj_cestica_pm2_5": "living_room",
    "broj_cestica_pm4": "living_room",
    "broj_cestica_pm10": "living_room",
    "prosjecna_velicina_cestica": "living_room",
    "sps30_pokreni_ciscenje": "living_room",
    # --- power measurement belongs to the house, not a room ---
    "mrezni_napon": "kuca",
    # Channel 1 is the upstairs feed, channel 2 the incomer. They were named
    # the other way round until 2026-09-12, when live readings settled which
    # clamp was on what; the old "test_*" entities are from that arrangement.
    "kat_struja": "kuca",
    "kat_stvarna_snaga": "kuca",
    "kat_prividna_snaga": "kuca",
    "kat_faktor_snage": "kuca",
    "kuca_ukupno_struja": "kuca",
    "kuca_ukupno_stvarna_snaga": "kuca",
    "kuca_ukupno_prividna_snaga": "kuca",
    "kuca_ukupno_faktor_snage": "kuca",
    "prizemlje_stvarna_snaga": "kuca",
    # Riemann-sum energy helpers, created 2026-09-12 to compare against the meter.
    "potrosnja_kuca": "kuca",
    "potrosnja_kat": "kuca",
    "potrosnja_prizemlje": "kuca",
    # House minus upstairs (template helper, 2026-09-13) - what the panel shows.
    "potrosnja_prizemlje_razlika": "kuca",
    "bme280_mux_uptime": "kuca",
    "bme280_mux_wifi_signal": "kuca",
    # --- lights ---
    "svjetlo_blagavaona": "blagavaona",
    "svjetlo_boravak": "living_room",
    "svjetlo_fotelja": "living_room",
    "svjetlo_tv": "living_room",
    "svjetlo_sank": "kitchen",
    "svjetlo_kuhinja": "kitchen",
    "svjetlo_kupaona": "kupaona",
    "svjetlo_hodnik": "hodnik",
    "svjetlo_soba1": "bedroom",
    "svjetlo_soba2": "bedroom",
    "svjetlo_terasa1": "terasa",
    "svjetlo_terasa2": "terasa",
    "svjetlo_ulaz": "ulaz",
    "svjetlo_vani": "vani",
    "svjetlo_stup": "vani",
    # The pump switch is by the entrance, not a house-wide thing.
    "svjetlo_hidrofor": "ulaz",
    # --- sockets ---
    "uticnica_blagavaona": "blagavaona",
    "uticnica_boravak": "living_room",
    "uticnica_tv": "living_room",
    "uticnica_kuhinja": "kitchen",
    "uticnica_frizider": "kitchen",
    "uticnica_kupaona": "kupaona",
    "uticnica_bojler": "kupaona",
    "uticnica_soba1": "bedroom",
    "uticnica_soba2": "bedroom",
    "uticnica_terasa": "terasa",
    "uticnica_ulaz": "ulaz",
    "pecnica": "kitchen",
}

# Whole entity ids that do not follow the suffix pattern.
ROOM_BY_ENTITY = {
    "media_player.smart_tv_pro": "living_room",
}

# Relays with nothing wired to them. Hidden rather than deleted: the channel
# exists on the board and will matter the day something is connected.
HIDE = [
    "switch.esp32_io_slobodno1",
    "switch.esp32_io_slobodno2",
]

# Diagnostics worth having on the System view. Everything else stays off.
ENABLE = [
    "sensor.home_assistant_core_cpu_percent",
    "sensor.home_assistant_core_memory_percent",
    "sensor.home_assistant_supervisor_cpu_percent",
    "sensor.home_assistant_supervisor_memory_percent",
    "sensor.home_assistant_host_disk_free",
    "sensor.home_assistant_host_disk_total",
    "sensor.home_assistant_host_disk_used",
    "sensor.home_assistant_operating_system_version",
    "update.esp32_io_firmware",
    "binary_sensor.mosquitto_broker_running",
    "binary_sensor.tailscale_running",
    "binary_sensor.esphome_device_builder_running",
    "binary_sensor.studio_code_server_running",
    "binary_sensor.samba_share_running",
    "binary_sensor.advanced_ssh_web_terminal_running",
    "binary_sensor.file_editor_running",
    "sensor.mobitel_wi_fi_connection",
    "sensor.mobitel_wi_fi_bssid",
]


async def call(ws, ident, payload):
    await ws.send(json.dumps({"id": ident, **payload}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == ident and msg.get("type") == "result":
            if not msg.get("success"):
                return {"GRESKA": msg.get("error")}
            return msg.get("result")


def target_area(entity_id: str) -> str | None:
    if entity_id in ROOM_BY_ENTITY:
        return ROOM_BY_ENTITY[entity_id]
    for suffix, area in ROOM_BY_SUFFIX.items():
        if entity_id.endswith(suffix):
            return area
    return None


def mqtt_duplicates(entities: list[dict]) -> list[str]:
    """MQTT copies of entities the ESPHome integration already provides.

    Matched by the `_2` Home Assistant appends when an id is taken, and only
    when the original really is an ESPHome entity -- so a genuinely new MQTT
    entity that happens to end in _2 is left alone.
    """
    esphome = {
        entry["entity_id"]
        for entry in entities
        if entry.get("platform") == "esphome"
    }
    return [
        entry["entity_id"]
        for entry in entities
        if entry.get("platform") == "mqtt"
        and entry["entity_id"].endswith("_2")
        and entry["entity_id"][: -len("_2")] in esphome
    ]


async def main():
    async with websockets.connect(URL, open_timeout=10, max_size=40_000_000) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth odbijen")

        entities = await call(ws, 1, {"type": "config/entity_registry/list"})
        known = {e["entity_id"]: e for e in entities}

        with open(BACKUP, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    e["entity_id"]: {
                        "area_id": e.get("area_id"),
                        "disabled_by": e.get("disabled_by"),
                        "hidden_by": e.get("hidden_by"),
                    }
                    for e in entities
                },
                fh,
                indent=2,
            )
        print(f"  kopija prije izmjena: {BACKUP} ({len(entities)} entiteta)")

        ident = 100

        async def update(entity_id: str, **changes):
            nonlocal ident
            ident += 1
            return await call(
                ws, ident,
                {"type": "config/entity_registry/update", "entity_id": entity_id, **changes},
            )

        moved = 0
        for entity_id, entry in sorted(known.items()):
            area = target_area(entity_id)
            if area is None or entry.get("area_id") == area or entry.get("disabled_by"):
                continue
            if "GRESKA" not in await update(entity_id, area_id=area):
                moved += 1
        print(f"  razmješteno po prostorijama: {moved}")

        hidden = 0
        for entity_id in HIDE:
            entry = known.get(entity_id)
            if entry is None or entry.get("hidden_by"):
                continue
            if "GRESKA" not in await update(entity_id, hidden_by="user"):
                hidden += 1
        print(f"  sakriveno (ništa spojeno): {hidden}")

        duplicates = mqtt_duplicates(entities)
        silenced = 0
        for entity_id in duplicates:
            if known[entity_id].get("disabled_by"):
                continue
            if "GRESKA" not in await update(entity_id, disabled_by="user"):
                silenced += 1
        print(f"  ugašenih MQTT dvojnika: {silenced} (od {len(duplicates)} nađenih)")

        enabled, failed = 0, []
        for entity_id in ENABLE:
            entry = known.get(entity_id)
            if entry is None:
                failed.append(entity_id)
                continue
            if not entry.get("disabled_by"):
                continue
            if "GRESKA" in await update(entity_id, disabled_by=None):
                failed.append(entity_id)
            else:
                enabled += 1
        print(f"  uključena dijagnostika: {enabled}")
        if failed:
            print("  nije uspjelo:", failed)


if __name__ == "__main__":  # importable, so the pure helpers can be tested
    asyncio.run(main())
