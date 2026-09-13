"""
Home Assistant senzorski alati — čitanje mjerenja iz HA (read-only).

Jarvis je do sada preko HA znao samo paliti/gasiti (TV) i MQTT sklopke; nije
imao čime *pročitati* nijedno mjerenje. ESPHome node `bme280-mux-node` (5x
BME280 po zonama, SPS30 kvaliteta zraka, SCT/ZMPT potrošnja) izlaže ~37
senzora u HA, pa ovi alati zatvaraju tu rupu.

Namjerno **samo čitanje**: nijedna funkcija ovdje ne poziva HA servis, pa
široki HA token ne može ovim putem pomaknuti bravu, alarm ili grijanje.

Klasifikacija ide po `device_class`/mjernoj jedinici, ne po imenu entiteta —
tako novi ESPHome node proradi bez ijedne izmjene koda.

Vrijednosti izvan fizikalno mogućeg raspona senzora označavaju se
`"sumnjivo": true`. BME280 na dugom I2C kablu zna vratiti smeće (viđeno
2026-08-20: kupaona 188.5 °C i -57.6 °C, uvijek iste dvije brojke — to su
registri senzora u reset stanju), a asistent koji to samouvjereno pročita
naglas je gori od asistenta koji kaže da mjerenje ne valja.

Kvaliteta zraka NEMA takvu provjeru: kod SPS30 je 1000 µg/m³ granica
specificirane točnosti, a ne granica stvarnosti — u zadimljenoj sobi očitanje
legitimno ode i preko 100000.

Env:
  HA_URL, HA_TOKEN        isti kao za TV alate
  HA_SENSOR_MAX_AGE_SECONDS  default 900 — starije mjerenje se označava zastarjelim
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from tools.adk_tools import _offload
from tools.adk_tools.ha_adk_tools import _ha_request

logger = logging.getLogger(__name__)

# device_class -> kategorija koju agent razumije
_CLIMATE_CLASSES = {"temperature", "humidity", "pressure", "atmospheric_pressure"}
_AIR_CLASSES = {"pm1", "pm25", "pm10", "pm4", "aqi", "carbon_dioxide", "volatile_organic_compounds"}
_POWER_CLASSES = {"power", "current", "voltage", "apparent_power", "power_factor", "energy"}

# Fizikalno mogući rasponi (senzorske specifikacije, ne "ugodne" vrijednosti).
# Izvan njih mjerenje je greška očitanja, ne stvarno stanje.
_PLAUSIBLE_RANGE = {
    "temperature": (-40.0, 85.0),      # BME280 spec
    # Lower bound is 0.1, not 0: the BME280 reset registers compensate to
    # exactly 0.0 % (seen 3x in the bathroom), and no real air is ever at 0 %.
    # 100 % stays valid — outdoors at dawn it is genuine.
    "humidity": (0.1, 100.0),
    # 300 hPa was far too generous: the BME280's failed reads land at ~496 and
    # ~519 hPa, well inside it, and were being reported as real. Sea-level
    # pressure never leaves 950-1050, so this still has a wide margin.
    "pressure": (850.0, 1085.0),
    "atmospheric_pressure": (850.0, 1085.0),
    "power_factor": (-1.0, 1.0),
    "voltage": (0.0, 500.0),
    "current": (0.0, 100.0),
}
# PM se namjerno NE provjerava rasponom. 1000 ug/m3 je granica *specificirane
# točnosti* SPS30, ne granica stvarnosti: korisnik je 2026-08-20 potvrdio da
# dim cigarete digne očitanje i preko 100000, a da mu kod kuće ionako stalno
# ima dima i prašine. Označiti to kao "greška očitanja" značilo bi lagati mu o
# vlastitom zraku. Visoke vrijednosti su stvarne — samo manje precizne.

_UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}


def _max_age_seconds() -> float:
    try:
        return float(os.getenv("HA_SENSOR_MAX_AGE_SECONDS", "900"))
    except ValueError:
        return 900.0


def _fetch_states() -> list:
    """Sva HA stanja; RuntimeError kad HA nije dostupan ili nije konfiguriran."""
    states = _ha_request("/api/states", timeout=15.0)
    return states or []


def _age_seconds(entry: dict) -> Optional[float]:
    stamp = entry.get("last_updated") or entry.get("last_changed")
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


_TRAILING_WORDS = {
    "temperature": {"temperatura"},
    "humidity": {"vlaga"},
    "pressure": {"tlak"},
    "atmospheric_pressure": {"tlak"},
    "current": {"struja"},
    "voltage": {"napon"},
    "power": {"snaga", "stvarna", "prividna"},
    "apparent_power": {"snaga", "prividna"},
    "power_factor": {"snage", "faktor"},
}


def _device_key(entity_id: str) -> str:
    """Gruba oznaka uređaja iz entity_id-a: "sensor.bme280_mux_node_x" -> "bme280_mux"."""
    object_id = entity_id.split(".", 1)[-1]
    return "_".join(object_id.split("_")[:2])


def _common_word_prefix(names: list) -> list:
    """Zajednički početak naziva — to je ime uređaja koje HA lijepi svakom entitetu."""
    if len(names) < 2:
        return []
    split_names = [n.split() for n in names]
    prefix = []
    for words in zip(*split_names):
        first = words[0]
        if any(w != first for w in words):
            break
        prefix.append(first)
    # Nikad ne pojedi cijeli naziv (svi entiteti istog imena).
    if any(len(w) <= len(prefix) for w in split_names):
        return prefix[:-1] if prefix else []
    return prefix


def _zone_from_name(friendly_name: str, device_class: str, prefix_len: int = 0) -> str:
    """Izvuci naziv zone iz HA friendly_name-a.

    "BME280 Mux Node Kupaona temperatura" -> "Kupaona": makni ime uređaja
    (prefiks zajednički svim entitetima istog uređaja) i naziv mjerenja.
    """
    trailing = _TRAILING_WORDS.get(device_class, set())
    # Kad uređaj ima malo entiteta, zajednički prefiks može progutati i zonu
    # ("Plug Kuhinja Struja" -> prefiks "Plug Kuhinja" -> prazno). Zato se
    # skraćuje dok ne ostane nešto smisleno.
    for take in range(prefix_len, -1, -1):
        words = friendly_name.split()[take:]
        while words and words[-1].lower() in trailing:
            words.pop()
        if words:
            return " ".join(words).strip()
    return "nepoznato"


def _zone_matches(needle: str, zona: str) -> bool:
    """Podudaranje zone otporno na hrvatske nastavke.

    "vanjska temperatura" i "vanjski tlak" su ista zona, pa se uspoređuju
    osnove riječi bez zadnjeg samoglasnika.
    """
    def stem(word: str) -> str:
        word = word.lower()
        return word[:-1] if len(word) > 3 and word[-1] in "aeiou" else word

    hay = " ".join(stem(w) for w in zona.split())
    return all(stem(w) in hay for w in needle.split())


def _reading(entry: dict) -> dict:
    """Jedno mjerenje u obliku koji agent može izravno prepričati."""
    attrs = entry.get("attributes", {})
    raw = str(entry.get("state", "")).strip()
    device_class = str(attrs.get("device_class", "")).lower()
    unit = attrs.get("unit_of_measurement", "")
    name = attrs.get("friendly_name", entry.get("entity_id", ""))

    out = {
        "entity_id": entry.get("entity_id", ""),
        "naziv": name,
        "jedinica": unit,
        "device_class": device_class,
    }

    if raw.lower() in _UNAVAILABLE_STATES:
        out["vrijednost"] = None
        out["dostupno"] = False
        out["napomena"] = "senzor ne javlja vrijednost"
        return out

    try:
        value = float(raw)
    except ValueError:
        out["vrijednost"] = raw
        out["dostupno"] = True
        return out

    out["vrijednost"] = round(value, 2)
    out["dostupno"] = True

    low, high = _PLAUSIBLE_RANGE.get(device_class, (None, None))
    if low is not None and not (low <= value <= high):
        out["sumnjivo"] = True
        out["napomena"] = (
            f"izvan mogućeg raspona senzora ({low}-{high} {unit}) — "
            "vjerojatno greška očitanja, ne stvarno stanje"
        )

    age = _age_seconds(entry)
    if age is not None:
        out["star_sekundi"] = int(age)
        if age > _max_age_seconds():
            out["zastarjelo"] = True
            out.setdefault("napomena", f"zadnje osvježenje prije {int(age // 60)} min")

    return out


def _collect(classes: set, zone: str = "") -> dict:
    try:
        states = _fetch_states()
    except RuntimeError as e:
        return {"error": str(e)}

    # Prvi prolaz: pokupi kandidate. Zona se ne može odrediti dok se ne zna
    # koji je dio naziva ime uređaja, a to se vidi tek iz cijele skupine.
    candidates = []
    for entry in states:
        entity_id = entry.get("entity_id", "")
        if not entity_id.startswith("sensor."):
            continue
        device_class = str(entry.get("attributes", {}).get("device_class", "")).lower()
        if device_class in classes:
            candidates.append((entity_id, device_class, entry))

    prefixes = {}
    for device, group in _group_by_device(candidates).items():
        names = [e.get("attributes", {}).get("friendly_name", "") for _, _, e in group]
        prefixes[device] = len(_common_word_prefix(names))

    zone_filter = zone.strip().lower()
    readings = []
    for entity_id, device_class, entry in candidates:
        reading = _reading(entry)
        reading["zona"] = _zone_from_name(
            reading["naziv"], device_class, prefixes.get(_device_key(entity_id), 0)
        )
        if zone_filter and not _zone_matches(zone_filter, reading["zona"]):
            continue
        readings.append(reading)

    if not readings:
        return {
            "mjerenja": [],
            "napomena": (
                f"nema senzora za zonu '{zone}'" if zone_filter
                else "nema senzora te vrste u Home Assistantu"
            ),
        }

    readings.sort(key=lambda r: (r["zona"], r["device_class"]))
    sumnjiva = [r["naziv"] for r in readings if r.get("sumnjivo")]
    result = {"mjerenja": readings, "broj": len(readings)}
    if sumnjiva:
        result["upozorenje"] = (
            "Ova mjerenja su izvan mogućeg raspona senzora i ne smiju se čitati "
            f"kao stvarno stanje: {', '.join(sumnjiva)}"
        )
    return result


def _group_by_device(candidates: list) -> dict:
    groups = {}
    for entity_id, device_class, entry in candidates:
        groups.setdefault(_device_key(entity_id), []).append((entity_id, device_class, entry))
    return groups


def home_climate_read(zone: str = "") -> dict:
    """
    Pročitaj temperaturu, vlagu i tlak po prostorijama (BME280 senzori).

    Args:
        zone: Naziv zone, npr. "kupaona", "soba", "dnevni", "ulaz", "vanjska".
              Prazno = sve zone.

    Returns:
        Mjerenja s jedinicama; polje "sumnjivo" označava neispravno očitanje.
    """
    return _collect(_CLIMATE_CLASSES, zone)


def home_air_quality_read() -> dict:
    """
    Pročitaj kvalitetu zraka (SPS30: PM1/PM2.5/PM4/PM10 i broj čestica).

    Returns:
        Mjerenja čestica u zraku s jedinicama.
    """
    return _collect(_AIR_CLASSES)


def home_power_read() -> dict:
    """
    Pročitaj potrošnju struje (SCT strujne kliješta, ZMPT napon).

    Returns:
        Struja, snaga, napon i faktor snage po mjernom kanalu.
    """
    return _collect(_POWER_CLASSES)


def home_sensor_search(query: str) -> dict:
    """
    Pretraži sve senzore u Home Assistantu po nazivu kad ne spadaju ni u jednu
    od gornjih kategorija (npr. WiFi signal, uptime, razina baterije).

    Args:
        query: Dio naziva senzora, npr. "wifi", "uptime", "baterija".

    Returns:
        Do 20 senzora čiji naziv sadrži traženi tekst.
    """
    needle = query.strip().lower()
    if not needle:
        return {"error": "Navedi što tražiš (npr. 'wifi', 'uptime')."}

    try:
        states = _fetch_states()
    except RuntimeError as e:
        return {"error": str(e)}

    hits = []
    for entry in states:
        entity_id = entry.get("entity_id", "")
        if not entity_id.startswith(("sensor.", "binary_sensor.")):
            continue
        name = str(entry.get("attributes", {}).get("friendly_name", entity_id))
        if needle in name.lower() or needle in entity_id.lower():
            hits.append(_reading(entry))
        if len(hits) >= 20:
            break

    if not hits:
        return {"mjerenja": [], "napomena": f"nijedan senzor ne odgovara '{query}'"}
    return {"mjerenja": hits, "broj": len(hits)}


async def _fetch_statistics_ws(ws_url: str, token: str, entity_ids: list,
                               start: str, period: str) -> dict:
    """One short-lived WebSocket round trip to HA's recorder."""
    import json as _json

    import websockets

    async with websockets.connect(ws_url, max_size=20_000_000, open_timeout=10) as ws:
        await ws.recv()
        await ws.send(_json.dumps({"type": "auth", "access_token": token}))
        auth = _json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant je odbio token")
        await ws.send(_json.dumps({
            "id": 1, "type": "recorder/statistics_during_period",
            "start_time": start, "statistic_ids": entity_ids,
            "period": period, "types": ["min", "max", "mean"],
        }))
        while True:
            message = _json.loads(await ws.recv())
            if message.get("id") == 1 and message.get("type") == "result":
                if not message.get("success"):
                    raise RuntimeError(str(message.get("error"))[:200])
                return message.get("result") or {}


def _run_coroutine(coro, timeout: float):
    """Run a coroutine from a synchronous tool, loop or no loop.

    ADK tools are plain functions, but every real caller reaches them from async
    code — the FastAPI web API, the Telegram handler, the voice loop. There
    asyncio.run() raises "cannot be called from a running event loop", which is
    exactly how this tool failed in production while passing when tried by hand
    from a synchronous script. When a loop is already running, the work goes to a
    thread that owns its own loop.
    """
    import asyncio
    import concurrent.futures

    def _blocking():
        return asyncio.run(asyncio.wait_for(coro, timeout))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _blocking()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_blocking).result(timeout + 5)


def _statistics(entity_ids: list, days: int, period: str = "day") -> dict:
    """Long-term statistics from HA's recorder, over its WebSocket API.

    The REST API cannot reach statistics -- only the WebSocket one can -- so this
    opens a short-lived socket per call. Worth it: without it the assistant can
    only ever say what a sensor reads *right now*, and "koja je danas bila
    najvisa temperatura" has no answer at all.
    """
    import asyncio
    import socket
    from datetime import datetime, timedelta, timezone as _tz

    url = os.getenv("HA_URL", "").strip().rstrip("/")
    token = os.getenv("HA_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("HA_URL/HA_TOKEN nisu postavljeni u .env")

    try:
        import websockets  # noqa: F401  (imported for the clearer error below)
    except ImportError as e:
        raise RuntimeError(f"websockets nije instaliran ({e})")

    ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    start = (datetime.now(_tz.utc) - timedelta(days=days)).isoformat()

    try:
        # The websocket path had its own fixed 25s, unconnected to the tool's
        # budget — so a 40s tool could spend 25 of them here after already
        # using 30. Same remaining budget as every other network call.
        return _run_coroutine(
            _fetch_statistics_ws(ws_url, token, entity_ids, start, period),
            _offload.clamp_timeout(25),
        )
    except RuntimeError:
        raise
    except (OSError, socket.gaierror, asyncio.TimeoutError) as e:
        raise RuntimeError(f"Home Assistant nedostupan ({e})")
    except Exception as e:
        raise RuntimeError(f"Statistika nije dostupna ({e})")


def home_climate_history(zone: str = "", days: int = 1) -> dict:
    """
    Najviša, najniža i prosječna temperatura kroz vrijeme (iz HA statistike).

    Koristi ovo za pitanja tipa "koja je danas bila najviša temperatura",
    "kolika je bila najniža u sobi", "kakav je bio tjedan" — trenutna
    očitanja (home_climate_read) na to ne mogu odgovoriti.

    Args:
        zone: Zona ("kupaona", "soba", "vanjska"...). Prazno = sve zone.
        days: Koliko dana unatrag (1 = danas, 7 = tjedan, 30 = mjesec).

    Returns:
        Po zoni: najviša, najniža i prosječna vrijednost s danima.
    """
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 1

    try:
        states = _fetch_states()
    except RuntimeError as e:
        return {"error": str(e)}

    candidates = []
    for entry in states:
        entity_id = entry.get("entity_id", "")
        attrs = entry.get("attributes", {})
        if entity_id.startswith("sensor.") and str(attrs.get("device_class", "")).lower() == "temperature":
            candidates.append((entity_id, "temperature", entry))

    prefixes = {}
    for device, group in _group_by_device(candidates).items():
        names = [e.get("attributes", {}).get("friendly_name", "") for _, _, e in group]
        prefixes[device] = len(_common_word_prefix(names))

    wanted = {}
    zone_filter = zone.strip().lower()
    for entity_id, _, entry in candidates:
        name = entry.get("attributes", {}).get("friendly_name", entity_id)
        zona = _zone_from_name(name, "temperature", prefixes.get(_device_key(entity_id), 0))
        if zone_filter and not _zone_matches(zone_filter, zona):
            continue
        wanted[entity_id] = zona

    if not wanted:
        return {"mjerenja": [], "napomena": f"nema temperaturnog senzora za zonu '{zone}'"}

    try:
        # Hourly, not daily: a single bad sample poisons that whole period's
        # min/max. At day resolution one 188.5 C glitch would wipe out the real
        # maximum for the entire day; at hour resolution only that hour is lost
        # and the other 23 still answer the question. HA keeps hourly long-term
        # statistics indefinitely, so this works for any range.
        stats = _statistics(list(wanted), days, period="hour")
    except RuntimeError as e:
        return {"error": str(e)}

    low, high = _PLAUSIBLE_RANGE["temperature"]

    def usable(values):
        # The recorder already stored the bad samples the ESP32 used to emit
        # (188.5 / -57.6 and friends). Reporting those back as "najviša danas"
        # would be worse than useless, so they are dropped here too — the
        # history is dirty for good, the answer does not have to be.
        return [v for v in values if v is not None and low <= v <= high]

    results = []
    for entity_id, zona in wanted.items():
        rows = stats.get(entity_id) or []
        raw_max = [r.get("max") for r in rows]
        raw_min = [r.get("min") for r in rows]
        values_max = usable(raw_max)
        values_min = usable(raw_min)
        values_mean = usable([r.get("mean") for r in rows])
        discarded = (len([v for v in raw_max if v is not None]) - len(values_max)
                     + len([v for v in raw_min if v is not None]) - len(values_min))

        if not values_max or not values_min:
            results.append({"zona": zona, "napomena": "nema zabilježene statistike za to razdoblje"})
            continue

        entry = {
            "zona": zona,
            "najvisa": round(max(values_max), 1),
            "najniza": round(min(values_min), 1),
            "prosjek": round(sum(values_mean) / len(values_mean), 1) if values_mean else None,
            "jedinica": "°C",
            "sati_podataka": len(rows),
        }
        if discarded:
            entry["odbaceno_neispravnih"] = discarded
            entry["napomena"] = (
                "u povijesti ima neispravnih očitanja senzora; preskočena su "
                "pri računanju"
            )
        results.append(entry)

    results.sort(key=lambda r: r["zona"])
    return {"razdoblje_dana": days, "mjerenja": results, "broj": len(results)}


def get_ha_sensor_tools() -> list:
    """Read-only senzorski alati za smart_home agenta.

    Offloaded like the TV tools — a sensor read is still a blocking urlopen,
    and home_climate_history can wait 30s for statistics. No device lock:
    these are reads, and queueing them behind a TV command would make the
    house slower to answer, not safer.
    """
    offloaded = _offload.offload(
        deadline_env="HA_SENSOR_DEADLINE_SECONDS", default_deadline=40.0,
    )
    return [offloaded(fn) for fn in (
        home_climate_read,
        home_air_quality_read,
        home_power_read,
        home_climate_history,
        home_sensor_search,
    )]
