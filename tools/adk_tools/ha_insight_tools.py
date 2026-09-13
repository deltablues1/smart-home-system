"""
Home Assistant uvid — sve što Home Assistant zna, samo za čitanje.

Senzorski alati (ha_sensor_tools) znaju klimu, zrak i struju u trenutku. Korisnik
je 2026-09-13 tražio da Jarvis zna *sve* što zna Home Assistant: što je upaljeno
po prostorijama, povijest bilo kojeg uređaja, logbook (što se kada upalilo),
potrošnju po danima, pa i HA-ove vlastite logove i zdravlje sustava.

Pravilo koje ovaj modul drži: **samo čitanje**. REST ide GET-om, osim
/api/template koji samo iscrta predložak, a WebSocket samo naredbama iz
_READ_ONLY_WS_COMMANDS. Isti token koji čita logbook može otključati bravu, pa
test provjerava izvorni kod ovog modula, ne samo njegovo ponašanje.

Vremena koja HA vrati su u UTC-u. Ovdje se prevode u lokalno vrijeme
(USER_TIMEZONE), jer "upaljeno u 10:04" po UTC-u je u Hrvatskoj krivo za sat ili
dva.

Env:
  HA_URL, HA_TOKEN                 isti kao za TV i senzorske alate
  USER_TIMEZONE                    default Europe/Zagreb
  HA_INSIGHT_DEADLINE_SECONDS      default 45
"""

import json
import logging
import os
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from tools.adk_tools import _offload
from tools.adk_tools.ha_adk_tools import _ha_request, _ha_token, _ha_url
from tools.adk_tools.ha_sensor_tools import _run_coroutine

logger = logging.getLogger(__name__)

# Every WebSocket command this module may send. Anything else is refused
# before a socket is opened.
_READ_ONLY_WS_COMMANDS = frozenset({
    "get_config",
    "config_entries/get",
    "system_log/list",
    "recorder/statistics_during_period",
})

_MAX_ENTITIES = 50
_MAX_EVENTS = 50
_MAX_CHANGES = 60
_MAX_STAT_ROWS = 72
_MAX_ATTR_CHARS = 1500

_UNAVAILABLE = {"unavailable", "unknown"}
# States that mean "this thing is currently doing something".
_ACTIVE_STATES = {"on", "open", "opening", "playing", "home", "heat", "cool", "detected"}
_ACTIVE_DOMAINS = ("light.", "switch.", "fan.", "media_player.", "climate.", "cover.")
# Buttons and events have no state until pressed or fired, so "unknown" there is
# normal. Counting them made "ESP32 IO Restart" look like a broken device.
_STATELESS_DOMAINS = ("button.", "event.", "scene.", "update.")


# --- helpers ---------------------------------------------------------------


def _fold(text: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("USER_TIMEZONE", "Europe/Zagreb"))
    except Exception:
        return ZoneInfo("Europe/Zagreb")


def _parse_time(stamp: Any) -> Optional[datetime]:
    """ISO string or epoch seconds/milliseconds -> aware UTC datetime."""
    if stamp is None or stamp == "":
        return None
    try:
        if isinstance(stamp, (int, float)):
            seconds = stamp / 1000.0 if stamp > 1e11 else float(stamp)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _local(stamp: Any, fmt: str = "%Y-%m-%d %H:%M") -> Optional[str]:
    parsed = _parse_time(stamp)
    return parsed.astimezone(_tz()).strftime(fmt) if parsed else None


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _get(path: str, timeout: float = 15.0) -> Any:
    """GET from the HA REST API. Raises RuntimeError when HA is unreachable."""
    return _ha_request(path, timeout=timeout)


def _render_template(template: str, timeout: float = 15.0) -> str:
    """Render a Jinja template in Home Assistant. Rendering changes nothing."""
    url, token = _ha_url(), _ha_token()
    if not url or not token:
        raise RuntimeError("HA_URL/HA_TOKEN nisu postavljeni u .env")
    request = urllib.request.Request(
        f"{url}/api/template",
        data=json.dumps({"template": template}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_offload.clamp_timeout(timeout)) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"HA predložak nije dostupan ({e})")


async def _ws_round_trip(ws_url: str, token: str, commands: List[dict]) -> List[Any]:
    import websockets

    results = []
    async with websockets.connect(ws_url, max_size=20_000_000, open_timeout=10) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant je odbio token")
        for number, command in enumerate(commands, start=1):
            await ws.send(json.dumps({"id": number, **command}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == number and message.get("type") == "result":
                    break
            if not message.get("success"):
                raise RuntimeError(str(message.get("error"))[:200])
            results.append(message.get("result"))
    return results


def _ws(commands: List[dict]) -> List[Any]:
    """Run read-only WebSocket commands, in order, over one short connection."""
    for command in commands:
        if command.get("type") not in _READ_ONLY_WS_COMMANDS:
            raise RuntimeError(f"naredba '{command.get('type')}' nije dopuštena (samo čitanje)")
    url, token = _ha_url(), _ha_token()
    if not url or not token:
        raise RuntimeError("HA_URL/HA_TOKEN nisu postavljeni u .env")
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    try:
        return _run_coroutine(_ws_round_trip(ws_url, token, commands), _offload.clamp_timeout(25))
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Home Assistant nedostupan ({e})")


_AREAS_TEMPLATE = (
    "{% for a in areas() %}{{ a }}|{{ area_name(a) }}|"
    "{{ area_entities(a) | join(',') }};;{% endfor %}"
)


def _areas() -> Dict[str, dict]:
    """area_id -> {"naziv": name, "entiteti": [entity_id, ...]}."""
    raw = _render_template(_AREAS_TEMPLATE)
    areas = {}
    for chunk in raw.split(";;"):
        parts = chunk.strip().split("|")
        if len(parts) != 3 or not parts[0]:
            continue
        area_id, name, entities = parts
        areas[area_id] = {
            "naziv": name,
            "entiteti": [e for e in entities.split(",") if e],
        }
    return areas


def _entity_area_map(areas: Dict[str, dict]) -> Dict[str, str]:
    mapping = {}
    for area in areas.values():
        for entity_id in area["entiteti"]:
            mapping[entity_id] = area["naziv"]
    return mapping


def _name(entry: dict) -> str:
    return str(entry.get("attributes", {}).get("friendly_name") or entry.get("entity_id", ""))


def _brief(entry: dict, area_of: Dict[str, str]) -> dict:
    attrs = entry.get("attributes", {})
    out = {
        "entity_id": entry.get("entity_id", ""),
        "naziv": _name(entry),
        "stanje": entry.get("state"),
    }
    if attrs.get("unit_of_measurement"):
        out["jedinica"] = attrs["unit_of_measurement"]
    area = area_of.get(entry.get("entity_id", ""))
    if area:
        out["podrucje"] = area
    changed = _local(entry.get("last_changed"))
    if changed:
        out["promijenjeno"] = changed
    return out


def _area_matches(query: str, area_id: str, area: dict) -> bool:
    q = _fold(query).strip()
    return bool(q) and (q in _fold(area["naziv"]) or q in _fold(area_id))


def _is_active(entry: dict) -> bool:
    entity_id = entry.get("entity_id", "")
    state = str(entry.get("state", "")).lower()
    if entity_id.startswith(_ACTIVE_DOMAINS):
        return state in _ACTIVE_STATES
    if entity_id.startswith("binary_sensor."):
        device_class = str(entry.get("attributes", {}).get("device_class", ""))
        return state == "on" and device_class in {
            "door", "window", "opening", "garage_door", "motion", "occupancy", "presence",
        }
    return False


def _is_dead(entry: dict) -> bool:
    return (
        str(entry.get("state", "")).lower() in _UNAVAILABLE
        and not entry.get("entity_id", "").startswith(_STATELESS_DOMAINS)
    )


def _error(e: Exception) -> dict:
    return {"error": str(e)}


# --- tools -----------------------------------------------------------------


def ha_house_overview(area: str = "") -> dict:
    """
    Pregled kuće iz Home Assistanta po prostorijama: što je upaljeno ili otvoreno,
    što ne radi (nedostupno) i koliko uređaja ima u svakoj prostoriji.

    Koristi za "što je upaljeno u kući", "je li sve ugašeno u kupaoni",
    "što ne radi", "koji uređaji su u dnevnom boravku".

    Args:
        area: Prostorija, npr. "kupaona", "dnevni boravak". Prazno = cijela kuća.

    Returns:
        Po prostoriji: aktivno (upaljeno/otvoreno/svira), nedostupno, broj uređaja.
    """
    try:
        states = _get("/api/states") or []
        areas = _areas()
    except RuntimeError as e:
        return _error(e)

    by_id = {s.get("entity_id"): s for s in states}
    wanted = {
        area_id: data for area_id, data in areas.items()
        if not area.strip() or _area_matches(area, area_id, data)
    }
    if area.strip() and not wanted:
        return {
            "error": f"nema prostorije '{area}'",
            "prostorije": [a["naziv"] for a in areas.values()],
        }

    rooms = []
    for data in wanted.values():
        entries = [by_id[e] for e in data["entiteti"] if e in by_id]
        active = [_name(e) for e in entries if _is_active(e)]
        dead = [_name(e) for e in entries if _is_dead(e)]
        rooms.append({
            "prostorija": data["naziv"],
            "broj_entiteta": len(entries),
            "aktivno": active[:15],
            "nedostupno": dead[:15],
        })
    rooms.sort(key=lambda r: r["prostorija"])

    result: Dict[str, Any] = {"prostorije": rooms}
    if not area.strip():
        assigned = {e for data in areas.values() for e in data["entiteti"]}
        result["ukupno_entiteta"] = len(states)
        result["bez_prostorije"] = len([s for s in states if s.get("entity_id") not in assigned])
        result["ukupno_nedostupno"] = len([s for s in states if _is_dead(s)])
    return result


def ha_find_entities(query: str = "", area: str = "", domain: str = "",
                     state: str = "", limit: int = 30) -> dict:
    """
    Pronađi bilo koji entitet u Home Assistantu i njegovo trenutno stanje.

    Pokriva sve što HA ima: svjetla i utičnice, senzore, tipkala, TV, mobitel,
    ažuriranja, automatizacije, vremensku prognozu... Kombiniraj filtere.

    Args:
        query: Dio naziva ili entity_id-a, npr. "bojler", "baterija", "uptime".
        area: Prostorija, npr. "kuhinja".
        domain: Vrsta, npr. "light", "switch", "sensor", "binary_sensor",
                "update", "automation", "media_player", "person".
        state: Samo entiteti u tom stanju, npr. "on", "off", "unavailable".
        limit: Najviše rezultata (do 50).

    Returns:
        Entiteti s nazivom, stanjem, jedinicom, prostorijom i vremenom zadnje promjene.
    """
    limit = _clamp(limit, 1, _MAX_ENTITIES, 30)
    try:
        states = _get("/api/states") or []
    except RuntimeError as e:
        return _error(e)
    try:
        areas = _areas()
    except RuntimeError:
        if area.strip():
            raise
        areas = {}  # rooms are a nicety here; the entities still answer

    area_of = _entity_area_map(areas)
    area_members = None
    if area.strip():
        area_members = {
            e for area_id, data in areas.items() if _area_matches(area, area_id, data)
            for e in data["entiteti"]
        }
        if not area_members:
            return {"error": f"nema prostorije '{area}'",
                    "prostorije": [a["naziv"] for a in areas.values()]}

    q, dom, st = _fold(query).strip(), domain.strip().lower().rstrip("."), _fold(state).strip()
    hits = []
    for entry in states:
        entity_id = entry.get("entity_id", "")
        if dom and not entity_id.startswith(dom + "."):
            continue
        if area_members is not None and entity_id not in area_members:
            continue
        if st and _fold(entry.get("state")) != st:
            continue
        if q and q not in _fold(_name(entry)) and q not in _fold(entity_id):
            continue
        hits.append(entry)

    hits.sort(key=lambda e: e.get("entity_id", ""))
    result = {
        "entiteti": [_brief(e, area_of) for e in hits[:limit]],
        "ukupno_pogodaka": len(hits),
    }
    if len(hits) > limit:
        result["napomena"] = f"prikazano prvih {limit}; suzi upit za ostale"
    if not hits:
        result["napomena"] = "ništa ne odgovara upitu"
    return result


def ha_entity_details(entity_id: str) -> dict:
    """
    Sve što Home Assistant zna o jednom entitetu: stanje, sve atribute,
    prostoriju i kada se zadnji put promijenio.

    Args:
        entity_id: Točan entity_id, npr. "switch.esp32_io_uticnica_bojler".
                   Ako ga ne znaš, prvo pozovi ha_find_entities.

    Returns:
        Stanje, atributi, prostorija, zadnja promjena i zadnje osvježenje.
    """
    entity_id = entity_id.strip()
    if "." not in entity_id:
        return {"error": "treba točan entity_id (npr. 'sensor.x'); potraži ga s ha_find_entities"}
    try:
        entry = _get(f"/api/states/{urllib.parse.quote(entity_id)}")
    except RuntimeError as e:
        if "404" in str(e):
            return {"error": f"Home Assistant nema entitet '{entity_id}'"}
        return _error(e)
    if not entry:
        return {"error": f"Home Assistant nema entitet '{entity_id}'"}

    try:
        area_of = _entity_area_map(_areas())
    except RuntimeError:
        area_of = {}

    attrs = {k: v for k, v in (entry.get("attributes") or {}).items() if k != "entity_picture"}
    text = json.dumps(attrs, ensure_ascii=False, default=str)
    if len(text) > _MAX_ATTR_CHARS:
        attrs = {"skraceno": text[:_MAX_ATTR_CHARS] + "…"}

    out = _brief(entry, area_of)
    out["atributi"] = attrs
    updated = _local(entry.get("last_updated"))
    if updated:
        out["osvjezeno"] = updated
    return out


def ha_state_history(entity_id: str, hours: int = 24) -> dict:
    """
    Povijest stanja jednog entiteta: kada se palio i gasio, kako se mijenjala
    vrijednost, koliko je dugo bio upaljen.

    Koristi za "kad se zadnji put upalio bojler", "koliko je dugo radila pećnica",
    "kako se mijenjala vlaga danas", "je li netko palio svjetlo noću".

    Args:
        entity_id: Točan entity_id (potraži ga s ha_find_entities).
        hours: Koliko sati unatrag (1-720).

    Returns:
        Promjene s lokalnim vremenom (najnovije zadnje) i sažetak:
        za uključeno/isključeno ukupno vrijeme uključenosti, za brojke min/max.
    """
    entity_id = entity_id.strip()
    if "." not in entity_id:
        return {"error": "treba točan entity_id; potraži ga s ha_find_entities"}
    hours = _clamp(hours, 1, 720, 24)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    query = urllib.parse.urlencode({
        "filter_entity_id": entity_id,
        "end_time": now.isoformat(),
    })
    try:
        raw = _get(
            f"/api/history/period/{urllib.parse.quote(start.isoformat())}"
            f"?{query}&minimal_response&no_attributes",
            timeout=25.0,
        )
    except RuntimeError as e:
        return _error(e)

    rows = (raw or [[]])[0] if raw else []
    changes = []
    for row in rows:
        when = _parse_time(row.get("last_changed") or row.get("last_updated"))
        if when is not None:
            changes.append((max(when, start), str(row.get("state"))))
    changes.sort(key=lambda c: c[0])
    if not changes:
        return {"entity_id": entity_id, "sati": hours, "promjene": [],
                "napomena": "nema zabilježenih stanja u tom razdoblju"}

    summary: Dict[str, Any] = {"broj_promjena": max(0, len(changes) - 1)}
    numbers = []
    for _, value in changes:
        try:
            numbers.append(float(value))
        except ValueError:
            pass
    if numbers and len(numbers) >= len(changes) / 2:
        summary.update({
            "najmanje": round(min(numbers), 2),
            "najvise": round(max(numbers), 2),
            "zadnje": numbers[-1],
        })
    else:
        active_seconds = 0.0
        switched_on = 0
        for index, (when, value) in enumerate(changes):
            end = changes[index + 1][0] if index + 1 < len(changes) else now
            if value.lower() in _ACTIVE_STATES:
                active_seconds += max(0.0, (end - when).total_seconds())
                if index > 0:
                    switched_on += 1
        summary["ukupno_ukljuceno_min"] = round(active_seconds / 60, 1)
        summary["broj_ukljucivanja"] = switched_on
        summary["sada"] = changes[-1][1]

    shown = changes[-_MAX_CHANGES:]
    result = {
        "entity_id": entity_id,
        "sati": hours,
        "sazetak": summary,
        "promjene": [{"vrijeme": _local(w), "stanje": s} for w, s in shown],
    }
    if len(changes) > len(shown):
        result["napomena"] = f"prikazane zadnje {len(shown)} od {len(changes)} promjena"
    return result


def ha_logbook(hours: int = 6, entity_id: str = "", search: str = "") -> dict:
    """
    Logbook Home Assistanta: što se u kući događalo — paljenja, gašenja,
    pokretanja automatizacija, dolasci i odlasci, promjene stanja.

    Koristi za "što se događalo u kući", "tko je palio svjetlo u hodniku",
    "je li se bojler palio dok me nije bilo".

    Args:
        hours: Koliko sati unatrag (1-168).
        entity_id: Samo za jedan entitet (opcionalno).
        search: Samo događaji čiji naziv ili poruka sadrži ovaj tekst.

    Returns:
        Do 50 najnovijih događaja s lokalnim vremenom.
    """
    hours = _clamp(hours, 1, 168, 6)
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    path = f"/api/logbook/{urllib.parse.quote(start.isoformat())}"
    if entity_id.strip():
        path += "?" + urllib.parse.urlencode({"entity": entity_id.strip()})
    try:
        events = _get(path, timeout=25.0) or []
    except RuntimeError as e:
        return _error(e)

    needle = _fold(search).strip()
    rows = []
    for event in events:
        text = event.get("message") or (f"→ {event.get('state')}" if event.get("state") is not None else "")
        name = event.get("name") or event.get("entity_id") or ""
        if needle and needle not in _fold(name) and needle not in _fold(text):
            continue
        rows.append({
            "vrijeme": _local(event.get("when")),
            "naziv": name,
            "dogadaj": text,
            "entity_id": event.get("entity_id"),
            "_t": _parse_time(event.get("when")) or start,
        })
    rows.sort(key=lambda r: r["_t"], reverse=True)
    for row in rows:
        del row["_t"]

    result = {"sati": hours, "dogadaji": rows[:_MAX_EVENTS], "ukupno": len(rows)}
    if len(rows) > _MAX_EVENTS:
        result["napomena"] = f"prikazano {_MAX_EVENTS} najnovijih od {len(rows)}"
    if not rows:
        result["napomena"] = "nema događaja u tom razdoblju"
    return result


def ha_statistics(entity_id: str, days: int = 7, period: str = "day") -> dict:
    """
    Dugoročna statistika bilo kojeg brojčanog senzora iz Home Assistanta.
    Za potrošnju energije vraća koliko je potrošeno po danu (ili satu, tjednu,
    mjesecu); za mjerenja vraća najmanju, najveću i prosječnu vrijednost.

    Koristi za "koliko smo jučer potrošili struje", "potrošnja kata ovaj tjedan",
    "prosječna vlaga u kupaoni ovaj mjesec".

    Args:
        entity_id: entity_id senzora, ili dio naziva (npr. "potrosnja kuca")
                   pa alat sam nađe senzor.
        days: Koliko dana unatrag (1-365).
        period: "hour", "day", "week" ili "month".

    Returns:
        Redci po razdoblju s lokalnim datumom i ukupni zbroj za potrošnju.
    """
    days = _clamp(days, 1, 365, 7)
    period = period if period in {"5minute", "hour", "day", "week", "month"} else "day"

    try:
        states = _get("/api/states") or []
    except RuntimeError as e:
        return _error(e)

    target = entity_id.strip()
    by_id = {s.get("entity_id"): s for s in states}
    if target not in by_id:
        words = _fold(target).replace("_", " ").split()
        candidates = [
            s for s in states
            if s.get("entity_id", "").startswith("sensor.")
            and s.get("attributes", {}).get("state_class")
            and all(w in _fold(_name(s)) + " " + _fold(s.get("entity_id")).replace("_", " ") for w in words)
        ]
        if not candidates:
            return {"error": f"nema senzora sa statistikom za '{entity_id}'"}
        if len(candidates) > 1:
            return {
                "napomena": "više senzora odgovara; ponovi s točnim entity_id",
                "kandidati": [{"entity_id": c["entity_id"], "naziv": _name(c)} for c in candidates[:12]],
            }
        target = candidates[0]["entity_id"]

    entry = by_id.get(target, {})
    attrs = entry.get("attributes", {})
    unit = attrs.get("unit_of_measurement", "")
    cumulative = attrs.get("state_class") in {"total", "total_increasing"}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        (stats,) = _ws([{
            "type": "recorder/statistics_during_period",
            "start_time": start,
            "statistic_ids": [target],
            "period": period,
            "types": ["change", "min", "max", "mean"],
        }])
    except RuntimeError as e:
        return _error(e)

    rows = (stats or {}).get(target) or []
    if not rows:
        return {"entity_id": target, "napomena": "Home Assistant nema statistiku za to razdoblje"}

    date_fmt = "%Y-%m-%d %H:%M" if period in {"5minute", "hour"} else "%Y-%m-%d"
    out_rows = []
    total = 0.0
    for row in rows:
        item: Dict[str, Any] = {"od": _local(row.get("start"), date_fmt)}
        if cumulative and row.get("change") is not None:
            item["potroseno"] = round(row["change"], 3)
            total += row["change"]
        for key, label in (("min", "najmanje"), ("max", "najvise"), ("mean", "prosjek")):
            if row.get(key) is not None:
                item[label] = round(row[key], 2)
        out_rows.append(item)

    result: Dict[str, Any] = {
        "entity_id": target,
        "naziv": _name(entry) if entry else target,
        "jedinica": unit,
        "razdoblje": period,
        "redci": out_rows[-_MAX_STAT_ROWS:],
    }
    if cumulative:
        result["ukupno_potroseno"] = round(total, 3)
    if len(out_rows) > _MAX_STAT_ROWS:
        result["napomena"] = f"prikazano zadnjih {_MAX_STAT_ROWS} redaka; ukupno vrijedi za cijelo razdoblje"
    return result


def _log_entry(entry: dict) -> dict:
    messages = entry.get("message") or []
    if isinstance(messages, str):
        messages = [messages]
    return {
        "izvor": entry.get("name"),
        "razina": entry.get("level"),
        "poruka": [str(m)[:300] for m in messages[:3]],
        "puta": entry.get("count", 1),
        "zadnje": _local(entry.get("timestamp")),
        "prvo": _local(entry.get("first_occurred")),
    }


def ha_system_log(limit: int = 20, level: str = "", search: str = "") -> dict:
    """
    Upozorenja i greške iz loga Home Assistanta (najnovije prve).

    Koristi za "ima li grešaka u Home Assistantu", "zašto ne radi integracija X",
    "što javlja ESPHome".

    Args:
        limit: Najviše zapisa (do 50).
        level: "WARNING", "ERROR" ili "CRITICAL". Prazno = sve.
        search: Samo zapisi čiji izvor ili poruka sadrži ovaj tekst.

    Returns:
        Zapisi s izvorom, razinom, porukom, brojem ponavljanja i vremenom.
    """
    limit = _clamp(limit, 1, 50, 20)
    try:
        (entries,) = _ws([{"type": "system_log/list"}])
    except RuntimeError as e:
        return _error(e)

    wanted_level = level.strip().upper()
    needle = _fold(search).strip()
    rows = []
    for entry in entries or []:
        if wanted_level and str(entry.get("level", "")).upper() != wanted_level:
            continue
        text = " ".join(str(m) for m in (entry.get("message") or []))
        if needle and needle not in _fold(entry.get("name")) and needle not in _fold(text):
            continue
        rows.append(entry)
    rows.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)

    result = {"zapisi": [_log_entry(e) for e in rows[:limit]], "ukupno": len(rows)}
    if not rows:
        result["napomena"] = "nema takvih zapisa u logu"
    return result


def ha_system_health() -> dict:
    """
    Zdravlje Home Assistanta u jednom pozivu: verzija, integracije koje se nisu
    učitale, uređaji koji ne javljaju stanje, dostupna ažuriranja i koliko je
    upozorenja i grešaka u logu.

    Koristi za "je li sve u redu s Home Assistantom", "što ne radi", "ima li
    ažuriranja".

    Returns:
        Sažetak stanja sustava s popisom problema.
    """
    try:
        states = _get("/api/states") or []
        config, entries, log = _ws([
            {"type": "get_config"},
            {"type": "config_entries/get"},
            {"type": "system_log/list"},
        ])
    except RuntimeError as e:
        return _error(e)

    # A disabled integration is a choice, not a fault.
    broken = [
        {"integracija": e.get("domain"), "naziv": e.get("title"), "stanje": e.get("state")}
        for e in entries or []
        if e.get("state") != "loaded" and not e.get("disabled_by")
    ]
    dead = [_name(s) for s in states if _is_dead(s)]
    updates = [
        {
            "naziv": _name(s),
            "instalirano": s.get("attributes", {}).get("installed_version"),
            "dostupno": s.get("attributes", {}).get("latest_version"),
        }
        for s in states if s.get("entity_id", "").startswith("update.") and s.get("state") == "on"
    ]
    levels: Dict[str, int] = {}
    for entry in log or []:
        levels[str(entry.get("level"))] = levels.get(str(entry.get("level")), 0) + 1
    worst = sorted(
        log or [],
        key=lambda e: ({"CRITICAL": 0, "ERROR": 1}.get(str(e.get("level")), 2), -(e.get("count") or 1)),
    )[:5]

    problems = []
    if broken:
        problems.append(f"{len(broken)} integracija nije učitano")
    if dead:
        problems.append(f"{len(dead)} entiteta ne javlja stanje")
    if levels.get("ERROR") or levels.get("CRITICAL"):
        problems.append(f"{levels.get('ERROR', 0) + levels.get('CRITICAL', 0)} vrsta grešaka u logu")

    return {
        "verzija": (config or {}).get("version"),
        "sve_u_redu": not problems,
        "problemi": problems,
        "integracije_s_problemom": broken,
        "broj_integracija": len(entries or []),
        "nedostupni_entiteti": dead[:20],
        "azuriranja": updates,
        "log_po_razini": levels,
        "najcesci_zapisi_u_logu": [_log_entry(e) for e in worst],
    }


def get_ha_insight_tools() -> list:
    """Read-only Home Assistant tools for the smart_home agent.

    Offloaded like the sensor tools: every call is a blocking HTTP or
    WebSocket round trip, and a statistics query can take seconds.
    """
    offloaded = _offload.offload(
        deadline_env="HA_INSIGHT_DEADLINE_SECONDS", default_deadline=45.0,
    )
    return [offloaded(fn) for fn in (
        ha_house_overview,
        ha_find_entities,
        ha_entity_details,
        ha_state_history,
        ha_logbook,
        ha_statistics,
        ha_system_log,
        ha_system_health,
    )]
