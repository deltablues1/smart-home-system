"""Deterministic voice weather via Open-Meteo (free, no API key).

Weather questions must not fall into voice_qa (tool-less, would answer from
parametric knowledge) nor the minutes-slow orchestrator. This service answers
in one or two fast HTTP calls: optional geocoding for a spoken location, then
the forecast. Parsing and formatting are pure functions so unit tests need no
network; only get_voice_weather_report touches HTTP.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Optional

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECONDS = 8.0

# WMO weather interpretation codes -> spoken Croatian description.
WMO_DESCRIPTIONS = {
    0: "vedro",
    1: "pretežno vedro",
    2: "djelomično oblačno",
    3: "oblačno",
    45: "magla",
    48: "magla",
    51: "rosulja",
    53: "rosulja",
    55: "jača rosulja",
    56: "ledena rosulja",
    57: "ledena rosulja",
    61: "slaba kiša",
    63: "kiša",
    65: "jaka kiša",
    66: "ledena kiša",
    67: "ledena kiša",
    71: "slab snijeg",
    73: "snijeg",
    75: "jak snijeg",
    77: "snježna zrnca",
    80: "slabi pljuskovi",
    81: "pljuskovi",
    82: "jaki pljuskovi",
    85: "snježni pljuskovi",
    86: "snježni pljuskovi",
    95: "grmljavinsko nevrijeme",
    96: "grmljavina s tučom",
    99: "grmljavina s tučom",
}

DAY_LABELS = {0: "danas", 1: "sutra", 2: "prekosutra"}

# Words that follow "za"/"u" in weather questions but are not place names
# ("kakvo je vrijeme za vikend", "hoće li kiša u petak").
_LOCATION_STOPWORDS = {
    "sutra", "danas", "prekosutra", "vikend", "vani", "veceras", "jutro",
    "ujutro", "popodne", "podne", "noc", "nocas", "ponedjeljak", "utorak",
    "srijedu", "cetvrtak", "petak", "subotu", "nedjelju",
}

# "... za Zagreb" / "... u Splitu" at the end of the question.
_LOCATION_RE = re.compile(r"\b(?:za|u)\s+([a-z][a-z ]*?)\s*$")

# Locative -> nominative for common Croatian cities. Open-Meteo's fuzzy
# geocoder mishandles locatives ("Rijeci" resolves to Rečica, not Rijeka),
# so the frequent ones are mapped explicitly; the rest go through the raw
# form plus a trimmed-vowel fallback. ASCII-folded keys.
_HR_CITY_LOCATIVES = {
    "zagrebu": "zagreb",
    "splitu": "split",
    "rijeci": "rijeka",
    "osijeku": "osijek",
    "zadru": "zadar",
    "puli": "pula",
    "sibeniku": "sibenik",
    "dubrovniku": "dubrovnik",
    "varazdinu": "varazdin",
    "karlovcu": "karlovac",
    "sisku": "sisak",
    "vukovaru": "vukovar",
    "bjelovaru": "bjelovar",
    "samoboru": "samobor",
    "makarskoj": "makarska",
    "porecu": "porec",
    "rovinju": "rovinj",
    "opatiji": "opatija",
    "velikoj gorici": "velika gorica",
    "slavonskom brodu": "slavonski brod",
}


def nominative_candidates(name: str) -> list[str]:
    """Geocoder query candidates for a spoken (possibly locative) place name."""
    key = name.strip().lower()
    if key in _HR_CITY_LOCATIVES:
        return [_HR_CITY_LOCATIVES[key]]
    candidates = [name]
    if len(name) > 3 and name[-1] in "aeiou":
        candidates.append(name[:-1])
    return candidates


# In-memory geocode cache: the default location is looked up on every
# weather question otherwise.
_geocode_cache: dict[str, Optional[dict]] = {}


def normalize_voice_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def describe_wmo(code) -> str:
    try:
        return WMO_DESCRIPTIONS.get(int(code), "promjenjivo")
    except (TypeError, ValueError):
        return "promjenjivo"


def degrees_phrase(value: float) -> str:
    """Spoken Croatian degrees: 1 stupanj, 2-4 stupnja, 5+ stupnjeva."""
    n = round(value)
    prefix = "minus " if n < 0 else ""
    n = abs(n)
    if n % 100 in (11, 12, 13, 14):
        unit = "stupnjeva"
    elif n % 10 == 1:
        unit = "stupanj"
    elif n % 10 in (2, 3, 4):
        unit = "stupnja"
    else:
        unit = "stupnjeva"
    return f"{prefix}{n} {unit}"


def detect_day_offset(message: str) -> int:
    """0 = danas, 1 = sutra, 2 = prekosutra."""
    normalized = normalize_voice_text(message)
    # "prekosutra" contains "sutra", so it must be checked first.
    if "prekosutra" in normalized:
        return 2
    if "sutra" in normalized:
        return 1
    return 0


def extract_location(message: str) -> Optional[str]:
    """Spoken location after a trailing "za"/"u", or None for the default.

    STT gives inflected forms ("u Splitu"); geocoding handles part of that,
    the rest falls back to the configured default location.
    """
    normalized = normalize_voice_text(message).strip(" ?!.,")
    match = _LOCATION_RE.search(normalized)
    if not match:
        return None
    tokens = [t for t in match.group(1).split() if t not in _LOCATION_STOPWORDS]
    if not tokens:
        return None
    return " ".join(tokens)


def format_weather_report(place: str, day_offset: int, current: dict, daily: dict) -> str:
    """Spoken Croatian weather summary from Open-Meteo current+daily blocks."""
    idx = min(day_offset, len(daily.get("weather_code", [])) - 1)
    if idx < 0:
        raise ValueError("Open-Meteo response has no daily data")

    desc = describe_wmo(daily["weather_code"][idx])
    t_max = degrees_phrase(daily["temperature_2m_max"][idx])
    t_min = degrees_phrase(daily["temperature_2m_min"][idx])

    parts = []
    if day_offset == 0 and current:
        now_temp = degrees_phrase(current.get("temperature_2m", 0))
        now_desc = describe_wmo(current.get("weather_code"))
        parts.append(f"U mjestu {place} trenutno je {now_temp} i {now_desc}.")
        parts.append(f"Danas najviša {t_max}, najniža {t_min}.")
    else:
        day_label = DAY_LABELS.get(day_offset, "sutra")
        parts.append(f"{day_label.capitalize()} u mjestu {place}: {desc}, najviša {t_max}, najniža {t_min}.")

    precip = (daily.get("precipitation_probability_max") or [None] * (idx + 1))[idx]
    if precip is not None:
        parts.append(f"Vjerojatnost oborina {round(precip)} posto.")

    wind = (current or {}).get("wind_speed_10m")
    if day_offset == 0 and wind is not None and wind >= 30:
        parts.append(f"Puše jak vjetar, oko {round(wind)} kilometara na sat.")

    return " ".join(parts)


async def _fetch_json(url: str, params: dict) -> dict:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            return await response.json()


async def _geocode(name: str) -> Optional[dict]:
    """Resolve a place name to {name, latitude, longitude}, with cache."""
    key = name.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    result = None
    candidates = nominative_candidates(name)
    for candidate in candidates:
        data = await _fetch_json(
            GEOCODE_URL,
            {"name": candidate, "count": 1, "language": "hr"},
        )
        hits = data.get("results") or []
        if hits:
            hit = hits[0]
            result = {
                "name": hit.get("name", candidate),
                "latitude": hit["latitude"],
                "longitude": hit["longitude"],
            }
            break

    _geocode_cache[key] = result
    return result


async def get_voice_weather_report(message: str) -> Optional[str]:
    """Full pipeline: message -> spoken Croatian forecast, or None on failure."""
    default_location = os.getenv("VOICE_WEATHER_DEFAULT_LOCATION", "Zagreb").strip() or "Zagreb"
    try:
        place = None
        spoken_location = extract_location(message)
        if spoken_location:
            place = await _geocode(spoken_location)
        if place is None:
            place = await _geocode(default_location)
        if place is None:
            logger.warning("Weather geocode failed for %r and default %r", spoken_location, default_location)
            return None

        day_offset = detect_day_offset(message)
        forecast = await _fetch_json(
            FORECAST_URL,
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
        return format_weather_report(
            place["name"],
            day_offset,
            forecast.get("current") or {},
            forecast.get("daily") or {},
        )
    except Exception:
        logger.warning("Voice weather lookup failed", exc_info=True)
        return None
