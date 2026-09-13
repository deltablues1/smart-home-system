"""
Home Assistant ADK Tools — TV i media upravljanje preko HA REST API-ja.

Jarvis ne priča s uređajima direktno: kaže Home Assistantu što treba
(media_player/remote servisi), a HA odradi komunikaciju s TCL Google TV-om
preko Android TV Remote integracije. Paljenje TV-a je posebno: TCL u dubljem
standbyju ugasi upravljačke servise, pa tv_turn_on prvo šalje Wake-on-LAN
magic packet (mrežna kartica ga čuje i u dubokom snu preko žice), zatim
ponavlja HA turn_on dok se TV ne javi.

Env (Pi .env):
  HA_URL                  npr. http://homeassistant.local:8123
  HA_TOKEN                long-lived access token
  TV_MEDIA_PLAYER_ENTITY  default media_player.tv
  TV_REMOTE_ENTITY        default remote.tv
  TV_MAC                  default d0:65:b3:07:97:bc (TCL 55P7K, žica)
  TV_WAKE_RETRIES         default 4 (pokušaji HA turn_on nakon WoL)
"""

import json
import logging
import re
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from tools.adk_tools import _offload

logger = logging.getLogger(__name__)


def _ha_url() -> str:
    return os.getenv("HA_URL", "").strip().rstrip("/")


def _ha_token() -> str:
    return os.getenv("HA_TOKEN", "").strip()


def _tv_media_player() -> str:
    return os.getenv("TV_MEDIA_PLAYER_ENTITY", "media_player.tv").strip()


def _tv_remote() -> str:
    return os.getenv("TV_REMOTE_ENTITY", "remote.tv").strip()


def _tv_mac() -> str:
    return os.getenv("TV_MAC", "d0:65:b3:07:97:bc").strip()


def _ha_request(path: str, payload: Optional[dict] = None, timeout: float = 10.0):
    """Call the HA REST API; returns parsed JSON or raises RuntimeError."""
    url, token = _ha_url(), _ha_token()
    if not url or not token:
        raise RuntimeError("HA_URL/HA_TOKEN nisu postavljeni u .env")

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{url}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    # Never wait longer than the operation has left. A POST is a command, so
    # record it — that is what separates "nothing happened" from "we do not
    # know" when the answer is lost. A GET changes nothing, so losing its
    # answer is an ordinary error.
    is_command = data is not None
    timeout = _offload.clamp_timeout(timeout)
    if is_command:
        _offload.note_dispatch()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        # A 4xx is a rejection: understood, refused, nothing done. A 5xx is
        # not the same thing — Home Assistant got as far as trying, and a
        # service call can fail after it has already reached the device.
        detail = e.read().decode("utf-8")[:200]
        if is_command and e.code >= 500:
            raise _offload.OutcomeUnknown(f"HA API {e.code} usred naredbe: {detail}")
        raise RuntimeError(f"HA API {e.code}: {detail}")
    except _offload.HAOperationError:
        raise
    except Exception as e:
        if is_command and not _offload.proves_nothing_was_sent(e):
            # The command went out and the reply never came. The TV may
            # already have acted on it, so reporting a plain failure here
            # would invite the agent to send it a second time.
            raise _offload.OutcomeUnknown(f"HA ne odgovara ({e})")
        raise RuntimeError(f"HA API nedostupan ({e})")


def _ha_service(domain: str, service: str, entity_id: str, extra: Optional[dict] = None):
    payload = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    return _ha_request(f"/api/services/{domain}/{service}", payload)


def _ha_state(entity_id: str) -> str:
    try:
        state = _ha_request(f"/api/states/{entity_id}")
        return (state or {}).get("state", "unknown")
    except RuntimeError:
        return "unknown"


def _send_wol(mac: str) -> None:
    """Broadcast a Wake-on-LAN magic packet (twice, ports 9 and 7)."""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    for port in (9, 7):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", port))


def ha_call_service(domain: str, service: str, entity_id: str, data_json: str = "") -> dict:
    """Pozovi proizvoljni Home Assistant servis.

    Args:
        domain: HA domena servisa (npr. "media_player", "remote", "switch").
        service: naziv servisa (npr. "turn_on", "volume_set", "play_media").
        entity_id: ciljni entitet (npr. "media_player.tv").
        data_json: opcionalni dodatni parametri kao JSON string
            (npr. '{"volume_level": 0.2}').

    Returns:
        dict sa "success" i opisom rezultata ili "error".
    """
    try:
        extra = json.loads(data_json) if data_json.strip() else None
    except json.JSONDecodeError as e:
        return {"error": f"data_json nije valjan JSON: {e}"}
    try:
        _ha_service(domain, service, entity_id, extra)
        return {"success": True, "detail": f"{domain}.{service} -> {entity_id}"}
    except RuntimeError as e:
        return {"error": str(e)}


def tv_turn_on() -> dict:
    """Upali televizor (TCL u dnevnoj sobi).

    Robusna sekvenca: Wake-on-LAN magic packet (budi mrežnu karticu i iz
    dubokog sna), zatim HA turn_on s ponavljanjem dok se TV ne javi.

    Returns:
        dict sa "success"/"error" i stanjem TV-a.
    """
    entity = _tv_media_player()
    retries = int(os.getenv("TV_WAKE_RETRIES", "4"))
    try:
        _send_wol(_tv_mac())
    except Exception as e:
        logger.warning("WoL slanje nije uspjelo: %s", e)

    last_err = None
    for attempt in range(1, retries + 1):
        # Ask before another round, not after: four attempts at ~35s each
        # used to outlive any caller's patience by minutes.
        _offload.check_deadline("paljenje TV-a")
        try:
            _ha_service("media_player", "turn_on", entity)
            _ha_service("remote", "turn_on", _tv_remote())
        except RuntimeError as e:
            last_err = str(e)
        _offload.sleep(3)
        state = _ha_state(entity)
        if state not in ("unavailable", "unknown", "off"):
            return {"success": True, "state": state, "attempts": attempt}
        try:
            _send_wol(_tv_mac())
        except Exception:
            pass

    state = _ha_state(entity)
    if state not in ("unavailable", "unknown", "off"):
        return {"success": True, "state": state, "attempts": retries}
    return {
        "error": (
            "TV se ne javlja ni nakon Wake-on-LAN i ponovljenih pokušaja"
            + (f" (zadnja HA greška: {last_err})" if last_err else "")
            + ". Ako je TV dugo u standbyju, možda je u predubokom snu."
        )
    }


def tv_turn_off() -> dict:
    """Ugasi televizor.

    Returns:
        dict sa "success"/"error".
    """
    try:
        _ha_service("media_player", "turn_off", _tv_media_player())
        return {"success": True, "detail": "TV ugašen"}
    except RuntimeError as e:
        return {"error": str(e)}


def tv_volume(action: str, level: int = 0) -> dict:
    """Upravljaj glasnoćom televizora.

    Args:
        action: "up" (pojačaj), "down" (stišaj), "set" (postavi na level),
            "mute" (bez zvuka), "unmute" (vrati zvuk).
        level: za "set" — postotak 0-100.

    Returns:
        dict sa "success"/"error".
    """
    entity = _tv_media_player()
    try:
        if action == "up":
            for _ in range(2):
                _ha_service("media_player", "volume_up", entity)
        elif action == "down":
            for _ in range(2):
                _ha_service("media_player", "volume_down", entity)
        elif action == "set":
            # media_player.tv (Android TV Remote) reports VOLUME_STEP but NOT
            # VOLUME_SET — calling volume_set returns HTTP 500. Verified on the
            # TCL on 2026-08-20. So step towards the target instead.
            return _volume_step_to(entity, max(0, min(100, level)))
        elif action == "mute":
            _ha_service("media_player", "volume_mute", entity, {"is_volume_muted": True})
        elif action == "unmute":
            _ha_service("media_player", "volume_mute", entity, {"is_volume_muted": False})
        else:
            return {"error": f"Nepoznata akcija '{action}' (up/down/set/mute/unmute)"}
        return {"success": True, "detail": f"glasnoća: {action} {level if action == 'set' else ''}".strip()}
    except RuntimeError as e:
        return {"error": str(e)}


def _volume_level(entity: str) -> Optional[float]:
    """Current volume as 0.0-1.0, or None when the entity does not report it."""
    try:
        state = _ha_request(f"/api/states/{entity}")
    except RuntimeError:
        return None
    value = (state or {}).get("attributes", {}).get("volume_level")
    return float(value) if isinstance(value, (int, float)) else None


def _volume_step_to(entity: str, target_percent: int, max_steps: int = 30) -> dict:
    """Walk the volume to a target using up/down steps.

    Needed because the Android TV Remote entity has no absolute volume control.
    Re-reads the level each step: the TV decides its own step size, so counting
    blindly would overshoot.
    """
    target = target_percent / 100.0
    current = _volume_level(entity)
    if current is None:
        return {"error": "TV ne javlja trenutnu glasnoću, pa je ne mogu postaviti na točnu vrijednost."}

    tolerance = 0.02
    for _ in range(max_steps):
        # 30 steps, each spending up to two 10s HTTP timeouts, is ten minutes
        # of a frozen event loop. The deadline is what really bounds this
        # walk; max_steps only stops it overshooting.
        _offload.check_deadline("postavljanje glasnoće")
        if abs(current - target) <= tolerance:
            break
        _ha_service("media_player", "volume_up" if current < target else "volume_down", entity)
        updated = _volume_level(entity)
        if updated is None or updated == current:
            break  # TV stopped responding or hit its own limit
        current = updated

    reached = int(round(current * 100))
    return {
        "success": True,
        "detail": f"glasnoća ~{reached}%",
        "exact": abs(current - target) <= tolerance,
    }


def _apps_file() -> Path:
    return Path(os.getenv(
        "TV_APPS_FILE",
        str(Path(__file__).resolve().parents[2] / "config" / "tv_apps.json"),
    ))


def _load_learned_apps() -> dict:
    path = _apps_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Ne mogu pročitati %s: %s", path, e)
        return {}


def _save_learned_apps(apps: dict) -> None:
    path = _apps_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(apps, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _ha_activities() -> dict:
    """Apps configured in the HA Android TV Remote integration, if any.

    Empty by default — the integration only knows the apps a human added in its
    options, which is exactly why "upali A1 Xplore" found nothing.
    """
    try:
        state = _ha_request(f"/api/states/{_tv_remote()}")
    except RuntimeError:
        return {}
    activities = (state or {}).get("attributes", {}).get("activity_list") or []
    return {str(a).strip().lower(): str(a) for a in activities}


# Deep links for the big streaming apps. Anything else has to be learned from
# the TV itself (tv_learn_app) — HA's app list is empty unless a human fills it
# in, which is why "upali A1 Xplore TV" used to find nothing at all.
_DEEP_LINKS = {
    "youtube": "https://www.youtube.com",
    "netflix": "https://www.netflix.com/title",
    "hbo": "https://play.hbomax.com",
    "hbo max": "https://play.hbomax.com",
    "disney": "https://www.disneyplus.com",
    "disney+": "https://www.disneyplus.com",
    "spotify": "spotify://",
}


# Jarvis Launcher, sideloaded on the TV (deploy/tv_app_launcher). The TCL only
# launches an app when handed a URI some installed app claims, and A1 Xplore
# claims none — so this tiny app claims one and forwards. Set
# TV_USE_PROXY_LAUNCHER=false to go back to sending bare package names, which
# this TV silently ignores.
_PROXY_PACKAGE = "hr.jarvis.launcher"
_PROXY_SCHEME = "jarvis"


# When an app was last launched from here. A freshly started app is not ready
# for key presses: on 2026-08-22 tv_channel fired two seconds after A1 Xplore
# came up and the keys were lost. 12 seconds was still too short; 25 worked in a
# slow hand-run, so that is the default. The TV reports nothing about what an
# app is drawing, so this is a measured guess and nothing better is available.
_last_launch_at = 0.0


def _app_warmup_seconds() -> float:
    try:
        return float(os.getenv("TV_APP_WARMUP_SECONDS", "25"))
    except ValueError:
        return 25.0


def _await_app_ready() -> float:
    """Sleep out whatever remains of the warm-up after a recent launch."""
    if not _last_launch_at:
        return 0.0
    waited = time.monotonic() - _last_launch_at
    remaining = _app_warmup_seconds() - waited
    if remaining <= 0:
        return 0.0
    slept = _offload.sleep(remaining)
    return round(slept, 1)


def _proxy_enabled() -> bool:
    return os.getenv("TV_USE_PROXY_LAUNCHER", "true").lower() != "false"


def _as_activity(target: str) -> str:
    """What to actually send to the TV for a resolved target.

    Deep links go as they are. A bare package goes through the proxy, because
    the television does nothing at all with a package name.
    """
    if "://" in target or not _proxy_enabled():
        return target
    from urllib.parse import quote
    return f"{_PROXY_SCHEME}://open?pkg={quote(target)}"


def _resolve_app(app: str) -> Optional[str]:
    """Name -> something the TV can launch (deep link, package or activity)."""
    key = app.strip().lower()
    learned = {k.lower(): v for k, v in _load_learned_apps().items()}
    if key in learned:
        return learned[key]
    if key in _DEEP_LINKS:
        return _DEEP_LINKS[key]
    activities = _ha_activities()
    if key in activities:
        return activities[key]
    # Partial match against learned names ("a1" -> "a1 xplore tv").
    for name, target in learned.items():
        if key in name or name in key:
            return target
    return None


def tv_open_app(app: str) -> dict:
    """Otvori aplikaciju na televizoru.

    Args:
        app: naziv aplikacije — ugrađeno: "youtube", "netflix", "hbo max",
            "disney", "spotify"; plus sve što je naučeno preko tv_learn_app
            (npr. "a1 xplore tv"). Može i package ime ili deep-link URL.

    Returns:
        dict sa "success"/"error". Kad aplikacija nije poznata, vraća popis
        onoga što jest — bez izmišljanja.
    """
    raw = app.strip()
    target = _resolve_app(raw)

    if target is None:
        # A bare package name or URL is launchable as-is; a plain word is not.
        if "." in raw or "://" in raw:
            target = raw
        else:
            known = sorted(set(list(_DEEP_LINKS) + list(_load_learned_apps())))
            return {
                "error": f"Ne znam aplikaciju '{raw}'.",
                "poznate_aplikacije": known,
                "kako_dodati": (
                    "Otvori tu aplikaciju na TV-u daljinskim, pa reci "
                    f"'zapamti ovu aplikaciju kao {raw}' — tada je mogu paliti sam."
                ),
            }

    # A deep link goes as-is; a package has to travel through the proxy app.
    # `expected` stays the package either way — that is what the TV will report
    # as the foreground app once the launch lands.
    expected = target if "://" not in target else ""
    activity = _as_activity(target)

    try:
        before = (_ha_request(f"/api/states/{_tv_media_player()}") or {})             .get("attributes", {}).get("app_id", "")
    except RuntimeError:
        before = ""

    try:
        _ha_service("remote", "turn_on", _tv_remote(), {"activity": activity})
    except RuntimeError as e:
        return {"error": str(e)}

    # HTTP 200 means Home Assistant accepted the command, NOT that the TV did
    # anything. The Android TV launch command carries a URI that some installed
    # app must claim; a bare package name is accepted and silently ignored
    # (measured 2026-08-22: youtube.com opened YouTube, hr.a1.android.tv.xploretv
    # left the TV on its home screen). So watch the TV instead of trusting 200.
    if expected and before == expected:
        # The TV already reports this package, so a launch cannot be told apart
        # from doing nothing — and that reading goes stale: on 2026-08-22 it sat
        # on hr.a1.android.tv.xploretv for eight minutes while the screen showed
        # the home screen, and only reloading the integration corrected it.
        return {
            "nepotvrdivo": True,
            "detail": f"Poslao sam naredbu za '{raw}'.",
            "napomena": (
                "TV je i prije naredbe javljao tu aplikaciju, pa ne mogu potvrditi "
                "da sam je ja otvorio — a taj podatak zna zaostajati. Provjeri ekran."
            ),
            "poslano": target,
        }

    opened = _wait_for_app_change(before, expected=expected)

    if opened == _PROXY_PACKAGE:
        # The proxy came up and stayed there, which is how it reports that it
        # could not resolve the package (it shows the reason on screen).
        return {
            "error": f"Jarvis Launcher se otvorio ali nije uspio pokrenuti '{raw}'.",
            "poslano": activity,
            "zasto": "Aplikacija vjerojatno nije instalirana na TV-u pod tim package imenom.",
        }

    if opened is None:
        return {
            "error": (
                f"Poslao sam naredbu za '{raw}', ali TV je ostao na istom ekranu — "
                "nije se otvorila."
            ),
            "poslano": activity,
            "tv_pokazuje": before or "nepoznato",
            "zasto": (
                "Televizor pokreće aplikaciju samo preko poveznice koju ta "
                "aplikacija registrira; samo package ime nije dovoljno."
            ),
        }

    global _last_launch_at
    _last_launch_at = time.monotonic()
    return {"success": True, "detail": f"otvorena {raw}", "launched": activity, "app_id": opened}


def _wait_for_app_change(before: str, expected: str = "", timeout: float = None) -> Optional[str]:
    """Poll the TV until the foreground app changes; None if it never does.

    Returns the new app_id. With `expected` set (a package we asked for), only
    that exact package counts — otherwise any change does, which is the best we
    can do for deep links whose target package we do not know in advance.
    """
    if timeout is None:
        timeout = float(os.getenv("TV_APP_LAUNCH_TIMEOUT_SECONDS", "8"))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _offload.sleep(1.0)
        try:
            current = (_ha_request(f"/api/states/{_tv_media_player()}") or {})                 .get("attributes", {}).get("app_id", "")
        except RuntimeError:
            continue
        # A transition is the only honest evidence. Matching `expected` without
        # a change would also match a stale reading that never moved.
        if not current or current == before:
            continue
        if not expected or current == expected:
            return current
    return None


def tv_list_apps() -> dict:
    """Prikaži koje aplikacije Jarvis zna upaliti na TV-u.

    Returns:
        Ugrađene aplikacije, naučene aplikacije i one konfigurirane u Home
        Assistantu, plus koja je trenutno otvorena.
    """
    learned = _load_learned_apps()
    current = None
    try:
        state = _ha_request(f"/api/states/{_tv_media_player()}")
        current = (state or {}).get("attributes", {}).get("app_id")
    except RuntimeError:
        pass

    return {
        "ugradene": sorted(_DEEP_LINKS),
        "naucene": learned,
        "iz_home_assistanta": sorted(_ha_activities().values()),
        "trenutno_otvorena": current,
    }


def tv_learn_app(name: str, package: str = "") -> dict:
    """Zapamti aplikaciju koja je TRENUTNO otvorena na TV-u pod zadanim imenom.

    Home Assistant ne zna popis instaliranih aplikacija (lista je prazna dok je
    čovjek ručno ne popuni), ali TV javlja koja aplikacija je otvorena. Zato:
    korisnik otvori aplikaciju daljinskim, ovaj alat zapamti njezin package, i
    od tada je Jarvis može paliti sam.

    Args:
        name: kako će je korisnik zvati, npr. "a1 xplore tv".
        package: (neobavezno) package ime ako ga već znaš; inače se čita s TV-a.

    Returns:
        dict sa "success"/"error".
    """
    label = name.strip()
    if not label:
        return {"error": "Trebam ime pod kojim da zapamtim aplikaciju."}

    package = package.strip()
    if not package:
        try:
            state = _ha_request(f"/api/states/{_tv_media_player()}")
        except RuntimeError as e:
            return {"error": str(e)}
        package = (state or {}).get("attributes", {}).get("app_id") or ""

    if not package:
        return {"error": "TV ne javlja koja je aplikacija otvorena."}

    # Launcher and screensaver are what the TV reports when nothing is really
    # open; learning either would give the user an "app" that opens the home
    # screen. Seen live: com.google.android.apps.tv.dreamx (screensaver).
    if any(marker in str(package).lower() for marker in _NOT_AN_APP):
        return {
            "error": "Na TV-u je trenutno početni ekran ili screensaver, ne aplikacija.",
            "trenutno": package,
            "kako": f"Otvori '{label}' daljinskim, pa ponovi ovaj zahtjev.",
        }

    apps = _load_learned_apps()
    apps[label] = package
    try:
        _save_learned_apps(apps)
    except Exception as e:
        return {"error": f"Ne mogu spremiti popis aplikacija: {e}"}

    logger.info("Naučena TV aplikacija '%s' -> %s", label, package)
    return {
        "success": True,
        "detail": f"Zapamćeno: '{label}' = {package}",
        "package": package,
    }


def _youtube_first_result(query: str) -> tuple:
    """(video_id, title) of the first YouTube hit, or (None, None).

    Opening a search-results page only gets the user a list they still have to
    click through with the remote — which is why "nađi pjesmu" used to find the
    song but never play it. Resolving to a watch URL makes the TV autoplay.
    """
    from urllib.parse import quote_plus

    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "hr-HR,hr;q=0.9,en;q=0.8",
    })
    try:
        # Same budget as the rest of the operation: without this the search
        # could spend 15s that tv_play_youtube no longer had.
        with urllib.request.urlopen(
            request, timeout=_offload.clamp_timeout(15)
        ) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except _offload.HAOperationError:
        # Out of time. Nothing was played, and the caller must hear that
        # rather than "no results".
        raise
    except Exception as e:
        logger.warning("YouTube pretraga nije uspjela: %s", e)
        return None, None

    ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    if not ids:
        return None, None
    titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]{1,120})"', html)
    return ids[0], (titles[0] if titles else None)


def tv_play_youtube(query: str, play_first: bool = True) -> dict:
    """Pusti nešto s YouTubea na televizoru.

    Args:
        query: što tražiti (npr. "Radiohead Creep").
        play_first: True (zadano) pušta prvi rezultat; False samo otvori
            pretragu da korisnik bira daljinskim.

    Returns:
        dict sa "success"/"error" i naslovom onoga što je pušteno.
    """
    from urllib.parse import quote_plus

    video_id = title = None
    if play_first:
        video_id, title = _youtube_first_result(query)

    if video_id:
        target = f"https://www.youtube.com/watch?v={video_id}"
        detail = f"puštam: {title or query}"
    else:
        target = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        detail = (
            f"YouTube pretraga: {query}"
            if not play_first
            else f"Nisam uspio odabrati snimku za '{query}', otvaram pretragu — izaberi daljinskim."
        )

    try:
        _ha_service("remote", "turn_on", _tv_remote(), {"activity": target})
    except RuntimeError as e:
        return {"error": str(e)}

    return {
        "success": True,
        "detail": detail,
        "playing": bool(video_id),
        "naslov": title,
        "url": target,
    }


# What the TV reports when nothing is really open. Learning either would give
# the user an "app" that opens the home screen.
_NOT_AN_APP = ("launcherx", "tvlauncher", "dreamx", "daydream", "backdrop")


def _normalize_channel(name: str) -> str:
    """Fold a channel name to its comparable core: "HRT 1 HD" -> "hrt1"."""
    folded = re.sub(r"[^a-z0-9]", "", name.lower())
    return folded[:-2] if folded.endswith("hd") and len(folded) > 2 else folded


def _channels_file() -> Path:
    return Path(os.getenv(
        "TV_CHANNELS_FILE",
        str(Path(__file__).resolve().parents[2] / "config" / "tv_channels.json"),
    ))


def _load_channels() -> dict:
    path = _channels_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Ne mogu pročitati %s: %s", path, e)
        return {}


def _save_channels(channels: dict) -> None:
    path = _channels_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(channels, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def tv_learn_channel(name: str, number: int, app: str = "") -> dict:
    """Zapamti broj kanala pod imenom, da ga poslije možeš tražiti riječima.

    Args:
        name: kako ga korisnik zove, npr. "HRT 1".
        number: broj kanala u aplikaciji, npr. 101.
        app: (neobavezno) aplikacija u kojoj taj broj vrijedi, npr.
            "a1 xplore tv" — tada je Jarvis prvo otvori ako već nije otvorena.

    Returns:
        dict sa "success"/"error".
    """
    label = name.strip()
    if not label:
        return {"error": "Trebam ime kanala."}
    try:
        number = int(number)
    except (TypeError, ValueError):
        return {"error": f"'{number}' nije broj kanala."}
    if number <= 0 or number > 9999:
        return {"error": "Broj kanala mora biti između 1 i 9999."}

    channels = _load_channels()
    previous = channels.get(label) or {}
    # Keep the app the catalogue already knows when the user only gives a number.
    channels[label] = {"number": number, "app": app.strip() or previous.get("app", "")}
    try:
        _save_channels(channels)
    except Exception as e:
        return {"error": f"Ne mogu spremiti kanale: {e}"}

    logger.info("Naučen kanal '%s' = %s (app=%s)", label, number, app or "-")
    return {"success": True, "detail": f"Zapamćeno: {label} = {number}"}


def tv_list_channels() -> dict:
    """Prikaži naučene kanale.

    Returns:
        Mapa ime -> broj kanala i aplikacija u kojoj vrijedi.
    """
    channels = _load_channels()
    return {"kanali": channels, "broj": len(channels)}


def tv_channel(name: str, from_app_home: bool = False) -> dict:
    """Prebaci na kanal — po naučenom imenu ili izravno po broju.

    Otvara pripadnu aplikaciju ako je zapamćena uz kanal, pa otipka broj na
    daljinskom. Brojevi se šalju kao niz tipki jer TV nema pojam "kanal X",
    nego samo tipke.

    Args:
        name: ime kanala ("HRT 1") ili sam broj ("101").
        from_app_home: True kad je aplikacija tek otvorena i stoji na svojoj
            početnoj stranici — tada se prvo uđe u live TV (strelica desno pa
            OK), jer se na početnoj stranici brojevi ignoriraju.

    Returns:
        dict sa "success"/"error".
    """
    query = name.strip()
    if not query:
        return {"error": "Koji kanal?"}

    channels = _load_channels()
    entry = channels.get(query)
    if entry is None:
        # People say "HRT1" and "hrt 1"; the operator's list says "HRT 1 HD".
        # Normalising both sides beats maintaining an alias for every spelling.
        wanted = _normalize_channel(query)
        for label, data in channels.items():
            if _normalize_channel(label) == wanted:
                entry = data
                break
    if entry is None:
        for label, data in channels.items():
            if query.lower() in label.lower():
                entry = data
                break

    if entry is None:
        if query.isdigit():
            entry = {"number": int(query), "app": ""}
        else:
            return {
                "error": f"Ne znam kanal '{query}'.",
                "poznati_kanali": sorted(channels),
                "kako_dodati": f"Reci mi koji je broj tog kanala, npr. '{query} je 101'.",
            }

    raw_number = entry.get("number")
    number = "" if raw_number is None else str(raw_number).strip()
    if not number.isdigit():
        # Catalogue entries seeded from the operator's channel list know the
        # name but not the number — A1's public list does not publish numbers.
        return {
            "error": f"Znam kanal '{query}', ali ne znam njegov broj.",
            "kako_dodati": f"Pogledaj broj u aplikaciji pa mi reci: '{query} je <broj>'.",
        }

    app = (entry.get("app") or "").strip()
    opened_app = None
    if app:
        try:
            current = _ha_request(f"/api/states/{_tv_media_player()}")
            running = str((current or {}).get("attributes", {}).get("app_id", ""))
        except RuntimeError:
            running = ""
        wanted = _resolve_app(app)
        # Only launch when we are not already inside that app: relaunching
        # would throw the user back to the app's home screen.
        if wanted and wanted not in running:
            result = tv_open_app(app)
            if "error" in result:
                return {"error": f"Ne mogu otvoriti '{app}': {result['error']}"}
            opened_app = app
            _offload.sleep(float(os.getenv("TV_CHANNEL_APP_DELAY_SECONDS", "4")))

    # A just-launched app needs time to draw before it will accept a keypress,
    # whether the next step is navigation or the digits themselves. This applies
    # to every path, not only the from_app_home one.
    warmed = _await_app_ready()

    entered_live = False
    if from_app_home:
        # The app discards digits on its own landing page; this is the sequence
        # the user verified on the physical remote to reach live TV from there.
        try:
            _ha_service(
                "remote", "send_command", _tv_remote(),
                {
                    "command": ["DPAD_RIGHT", "DPAD_CENTER"],
                    "delay_secs": float(os.getenv("TV_CHANNEL_KEY_DELAY_SECONDS", "0.4")),
                },
            )
            _offload.sleep(float(os.getenv("TV_CHANNEL_LIVE_DELAY_SECONDS", "4")))
            entered_live = True
        except RuntimeError as e:
            return {"error": f"Ne mogu ući u live TV: {e}"}

    try:
        _ha_service(
            "remote", "send_command", _tv_remote(),
            {
                "command": list(number) + ["DPAD_CENTER"],
                "delay_secs": float(os.getenv("TV_CHANNEL_KEY_DELAY_SECONDS", "0.4")),
            },
        )
    except RuntimeError as e:
        return {"error": str(e)}

    detail = f"kanal {number}"
    if opened_app:
        detail += f" (otvorio {opened_app})"
    if entered_live:
        detail += " (prvo ušao u live TV)"
    if warmed:
        detail += f" (čekao {warmed}s da se aplikacija učita)"

    return {
        "success": True,
        "detail": detail,
        "broj": number,
        "iz_pocetne_stranice": entered_live,
        "napomena": (
            "Poslao sam brojeve na daljinski. Radi samo kad aplikacija već "
            "prikazuje neki kanal (live TV) — na početnoj stranici aplikacije "
            "brojevi se ignoriraju. Ne mogu vidjeti na čemu je aplikacija, pa "
            "provjeri ekran."
        ),
    }


def tv_send_key(key: str) -> dict:
    """Pošalji tipku daljinskog televizoru (navigacija).

    Args:
        key: jedna od DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, DPAD_CENTER,
            BACK, HOME, MEDIA_PLAY_PAUSE, MEDIA_NEXT, MEDIA_PREVIOUS,
            CHANNEL_UP, CHANNEL_DOWN.

    Returns:
        dict sa "success"/"error".
    """
    allowed = {
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
        "BACK", "HOME", "MEDIA_PLAY_PAUSE", "MEDIA_NEXT", "MEDIA_PREVIOUS",
        "CHANNEL_UP", "CHANNEL_DOWN",
    }
    key = key.strip().upper()
    if key not in allowed:
        return {"error": f"Nepodržana tipka '{key}'. Podržane: {', '.join(sorted(allowed))}"}
    try:
        _ha_service("remote", "send_command", _tv_remote(), {"command": key})
        return {"success": True, "detail": f"tipka {key}"}
    except RuntimeError as e:
        return {"error": str(e)}


def tv_status() -> dict:
    """Dohvati stanje televizora (upaljen/ugašen, što se reproducira).

    Returns:
        dict sa "state" i, ako postoji, "app" i "media_title".
    """
    try:
        state = _ha_request(f"/api/states/{_tv_media_player()}")
    except RuntimeError as e:
        return {"error": str(e)}
    if not state:
        return {"error": "TV entitet nije pronađen u Home Assistantu"}
    attrs = state.get("attributes", {})
    package = attrs.get("app_id") or ""
    known = [label for label, target in _load_learned_apps().items() if target == package]

    result = {
        "state": state.get("state", "unknown"),
        "app": attrs.get("app_name") or package or "",
        "package": package,
        "media_title": attrs.get("media_title", ""),
        "aplikacija_zapamcena": bool(known),
    }
    if known:
        result["zapamcena_kao"] = known[0]
    elif package and not any(m in package.lower() for m in _NOT_AN_APP):
        # Reading the package is not the same as storing it. Spelling that out
        # here because on 2026-08-20 the agent read this field and then told the
        # user the app had been remembered, while nothing was ever saved.
        result["upozorenje"] = (
            "Ova aplikacija NIJE zapamćena — sam podatak da je vidim ne znači da "
            "je spremljena. Za trajno pamćenje pozovi tv_learn_app(ime)."
        )
    return result


def get_ha_adk_tools() -> list:
    """Get all Home Assistant TV/media tools as a list.

    ha_call_service is deliberately NOT exposed to the LLM: an arbitrary
    domain/service/entity call could reach locks, alarm, garage or heating if
    the HA token is broad. Only typed, allowlisted tools go to the agent;
    ha_call_service stays importable for internal/typed wrappers.
    """
    tools = [
        tv_turn_on,
        tv_turn_off,
        tv_volume,
        tv_open_app,
        tv_list_apps,
        tv_learn_app,
        tv_play_youtube,
        tv_channel,
        tv_learn_channel,
        tv_list_channels,
        tv_send_key,
        tv_status,
    ]
    # Offload at registration, not on the functions themselves: the tools call
    # each other (tv_channel opens an app first), and those inner calls must
    # stay plain and synchronous — one thread, one deadline, one turn of the
    # device lock for the whole operation.
    #
    # The key is "tv", the device, not the entity: remote.tv and
    # media_player.tv are one television, and separate queues would let
    # "otvori aplikaciju" and "promijeni kanal" interleave on it again.
    wake = _offload.offload(
        device="tv",
        deadline_env="TV_WAKE_DEADLINE_SECONDS",
        default_deadline=90.0,  # WoL + up to TV_WAKE_RETRIES rounds
    )
    normal = _offload.offload(
        device="tv",
        deadline_env="TV_OP_DEADLINE_SECONDS",
        default_deadline=45.0,
    )
    return [(wake if fn is tv_turn_on else normal)(fn) for fn in tools]
