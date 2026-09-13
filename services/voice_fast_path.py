"""Fast local voice command handlers for low-latency smart-home actions."""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Optional

from tools.adk_tools.mqtt_adk_tools import mqtt_scene_control, mqtt_switch_control


SMART_HOME_ROOM_ALIASES = {
    "svjetlo_boravak": ("boravak", "dnevni", "dnevni boravak", "dnevnom boravku"),
    "svjetlo_kuhinja": ("kuhinja", "kuhinji"),
    "svjetlo_hodnik": ("hodnik", "hodniku"),
    "svjetlo_kupaona": ("kupaona", "kupaonici", "kupatilo", "kupatilu"),
    "svjetlo_blagavaona": ("blagavaona", "blagovaona", "blagavaonici", "blagovaonici"),
    "svjetlo_ulaz": ("ulaz",),
    "svjetlo_terasa1": ("terasa", "terasi"),
    "svjetlo_vani": ("vani", "dvoriste", "dvoristu"),
    "svjetlo_soba1": ("soba 1", "soba1"),
    "svjetlo_soba2": ("soba 2", "soba2"),
}

SMART_HOME_SPOKEN_NAMES = {
    "svjetlo_boravak": "svjetlo u dnevnom boravku",
    "svjetlo_kuhinja": "svjetlo u kuhinji",
    "svjetlo_hodnik": "svjetlo u hodniku",
    "svjetlo_kupaona": "svjetlo u kupaoni",
    "svjetlo_blagavaona": "svjetlo u blagovaonici",
    "svjetlo_ulaz": "svjetlo na ulazu",
    "svjetlo_terasa1": "svjetlo na terasi",
    "svjetlo_vani": "vanjsko svjetlo",
    "svjetlo_soba1": "svjetlo u sobi 1",
    "svjetlo_soba2": "svjetlo u sobi 2",
}

SMART_HOME_SCENE_ALIASES = {
    "nocno": ("nocno", "nocno"),
    "film": ("film", "kino"),
    "dolazak": ("dolazak", "dosao sam", "dosla sam"),
    "odlazak": ("odlazak", "idem van", "izlazim"),
    "kuhanje": ("kuhanje", "kuham", "kuhaj"),
}


def normalize_voice_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


# The fast path is substring-based, so a question ("koji film preporucujes?"),
# a negation ("nemoj ugasiti sve") or a future/conditional request ("sutra
# ugasi sve") would otherwise fire a real device action. The gate is a
# POSITIVE grammar: a message counts as a command only when it carries an
# imperative trigger AND no blocking marker AND is short. "blocked" is not a
# rejection — the request continues to the LLM smart_home agent; a false
# positive only costs the latency shortcut. Markers are matched against the
# diacritic-stripped text with a leading space = word start ("kad" must not
# match "nikad").
_FAST_PATH_BLOCK_MARKERS = (
    # questions (STT often drops the trailing "?")
    " koji", " koja", " koje", " kakav", " kakva", " kakvo", " sto ", " zasto",
    " je li", " jesu li", " jel ", " jeli ", " da li ", " dal ",
    " moze li", " mozes li", " hoces li", " hoce li", " preporuc", " reci",
    # negations / exceptions / compound sentences the matcher can't parse
    " nemoj", " ne gasi", " ne pali", " ne ukljuc", " ne iskljuc",
    " ne upal", " ne ugas", " nista ne ", " nikad", " osim", " ali ",
    " pa onda", " a onda", " zatim", ", a ",
    # brightness/percentage requests — the switch fast path would turn the
    # device fully ON instead of dimming (live incident: "...a stavi svjetlo
    # u fotelju na 60%" executed only the first half)
    " posto", " na pola", "%",
    # future / conditional / scheduling
    " sutra", " prekosutra", " kasnije", " veceras", " navecer", " ujutro", " za sat",
    " za pola sata", " za pet", " za deset", " za petnaest", " za dvadeset",
    " za dvije", " za tri", " za cetiri", " u ponoc", " kad ", " kada ",
    " ako ", " cim ", " nakon ",
    " podsjeti", " zakazi", " rasporedi",
)

# Time expressions ("u 22 sata ugasi sve", "u 7:30", "za 2 minute") and
# numeric levels ("na 60", "na 128") the ON/OFF fast path cannot honor.
_FAST_PATH_BLOCK_REGEXES = (
    re.compile(r" u \d+ sat"),
    re.compile(r" u \d+[:.]\d+"),
    re.compile(r" za \d+ (minut|sekund|sat)"),
    re.compile(r" na \d+"),
)

# Positive trigger: an imperative command verb. "stavi/postavi/namjesti"
# count as set-verbs so a compound "ugasi X, a stavi Y..." trips the
# conflicting-verbs check below instead of executing only half.
_ON_VERB_RE = re.compile(r"\b(upali(te)?|ukljuci(te)?|pali|stavi(te)?|postavi(te)?|namjesti(te)?)\b")
_OFF_VERB_RE = re.compile(r"\b(ugasi(te)?|iskljuci(te)?|gasi)\b")

# Scene activation verbs: a scene alias alone inside a longer sentence
# ("objasni film Inception") must NOT fire — it needs an activation verb
# or the utterance must BE the alias.
_SCENE_ACTIVATION_MARKERS = (
    "idemo", "gledati", "gledamo", "pusti", "aktiviraj", "ukljuci", "upali",
    "pokreni", "prebaci", "mod", "scena", "scen", "rezim",
    # per-scene natural phrases already carry intent:
    "dosao sam", "dosla sam", "idem van", "izlazim", "kuham", "kuhaj",
    "ugasi sve", "sve ugasi",
)

# Longer sentences carry context the substring matcher can't understand.
_FAST_PATH_MAX_TOKENS = 12


def _scene_alias_hit(normalized: str) -> bool:
    for aliases in SMART_HOME_SCENE_ALIASES.values():
        if any(alias in normalized for alias in aliases):
            return True
    return False


def _has_command_trigger(normalized: str) -> bool:
    has_on = bool(_ON_VERB_RE.search(normalized))
    has_off = bool(_OFF_VERB_RE.search(normalized))
    # Both directions in one sentence ("ugasi sve i upali kuhinju") — the
    # simple matcher would execute only half of it. Leave it to the LLM.
    if has_on and has_off:
        return False
    if has_on or has_off:
        return True

    # Scene alias: fire only when the utterance IS the alias (short exact
    # phrase) or an activation verb accompanies it — a bare "film" inside
    # "objasni film Inception" must not trigger the scene.
    stripped = normalized.strip()
    if not _scene_alias_hit(normalized):
        return False
    for aliases in SMART_HOME_SCENE_ALIASES.values():
        if stripped in aliases:
            return True
    return any(marker in normalized for marker in _SCENE_ACTIVATION_MARKERS)


def classify_fast_path_intent(normalized: str) -> str:
    """Classify normalized voice text as "command" or "blocked" for the fast path.

    "blocked" does NOT mean rejected — the request continues to the LLM
    smart-home agent; only the deterministic substring shortcut is skipped.
    """
    if "?" in normalized or '"' in normalized:
        return "blocked"
    stripped = normalized.strip()
    if len(stripped.split()) > _FAST_PATH_MAX_TOKENS:
        return "blocked"
    padded = f" {stripped} "
    if any(marker in padded for marker in _FAST_PATH_BLOCK_MARKERS):
        return "blocked"
    if any(rx.search(padded) for rx in _FAST_PATH_BLOCK_REGEXES):
        return "blocked"
    if not _has_command_trigger(padded):
        return "blocked"
    return "command"


def resolve_voice_smart_home_response(
    base_response: str,
    response_mode: Optional[str] = None,
) -> str:
    """Shorten a routine confirmation to the configured brevity.

    Only for an action that actually happened and was verified. "U redu." and
    silence both mean "done"; using them for anything else tells the user
    something that is not true.
    """
    mode = (
        response_mode
        or os.getenv("VOICE_SMART_HOME_RESPONSE_MODE", "ok")
    ).strip().lower()
    if mode == "none":
        return ""
    if mode == "ok":
        return "U redu."
    return base_response


def speak_in_full(base_response: str, response_mode: Optional[str] = None) -> str:
    """Say the whole sentence, whatever the brevity setting.

    The Pi runs VOICE_SMART_HOME_RESPONSE_MODE=ok, which answers every
    smart-home command with "U redu." That is right for a light that just came
    on, and wrong for everything else — on 2026-09-06 it flattened "svjetlo je
    već upaljeno" into the same two words as success, so a command that
    changed nothing sounded exactly like one that worked.

    Silence ("none") is the same problem: the user asked for no chatter on
    success, not for a wrong answer delivered quietly.
    """
    return base_response


# Only one status means "the device told us it did it".
_SUCCESS_STATUSES = ("confirmed",)

# The device did not report back; it was already sitting in the requested
# state as far as the broker knows. Usually that just means the light was
# already on. It is also what a device that has silently dropped off MQTT
# looks like, because its last state stays retained on the broker — so this
# must never be spoken as though we had just done something.
_ALREADY_STATUSES = ("already_in_state",)
_SENT_SUFFIX = " Napomena: potvrda stanja je isključena, uređaj nije provjeren."


def _scene_outcome_response(
    result: dict,
    success_text: str,
    response_mode: Optional[str],
) -> Optional[str]:
    """Spoken response for a scene result. Failures are ALWAYS spoken."""
    status = result.get("status")
    if status in _SUCCESS_STATUSES:
        return resolve_voice_smart_home_response(success_text, response_mode)
    if status in _ALREADY_STATUSES:
        return speak_in_full("Sve je već bilo u tom stanju.", response_mode)
    if status == "sent":
        return f"Poslao sam naredbe za scenu.{_SENT_SUFFIX}"
    if status == "partial":
        confirmed = result.get("confirmed", 0)
        total = result.get("total", 0)
        unconfirmed = result.get("unconfirmed_devices") or []
        if not confirmed:
            # Degraded scene: lead with the warning, never with "Uključio sam".
            return (
                "Pažnja: naredbe su poslane, ali nijedan uređaj nije potvrdio "
                "promjenu. Provjeri jesu li uređaji dostupni."
            )
        detail = f" Nisu potvrdili: {', '.join(unconfirmed[:4])}." if unconfirmed else ""
        return f"{success_text} Potvrđeno {confirmed} od {total} uređaja.{detail}"
    if status in ("timeout", "unconfirmed"):
        return (
            "Poslao sam naredbe, ali nijedan uređaj nije potvrdio promjenu. "
            "Provjeri jesu li uređaji dostupni."
        )
    if status == "error":
        return "Ne mogu se spojiti na pametnu kuću. Provjeri MQTT vezu."
    return None


async def execute_fast_smart_home_command(
    message: str,
    response_mode: Optional[str] = None,
) -> Optional[str]:
    """Run a deterministic smart-home command without the full orchestrator path."""
    normalized = normalize_voice_text(message)

    # Questions, negations and deferred requests must never fire devices from
    # the substring shortcut — let the LLM agent interpret them instead.
    if classify_fast_path_intent(normalized) == "blocked":
        return None

    if "ugasi sve" in normalized or "sve ugasi" in normalized:
        result = await mqtt_scene_control("sve_ugasi")
        return _scene_outcome_response(
            result, "Ugasio sam sve sto se smije ugasiti.", response_mode
        )

    for scene, aliases in SMART_HOME_SCENE_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            result = await mqtt_scene_control(scene)
            description = str(result.get("description") or scene).strip()
            return _scene_outcome_response(
                result, f"Ukljucio sam scenu {description}.", response_mode
            )

    state = None
    if any(token in normalized for token in ("upal", "upale", "ukljuc")):
        state = "ON"
    elif any(token in normalized for token in ("ugas", "iskljuc")):
        state = "OFF"

    if state is None:
        return None

    matched_devices = []
    for device_name, aliases in SMART_HOME_ROOM_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            matched_devices.append(device_name)

    if len(matched_devices) != 1:
        return None

    device_name = matched_devices[0]
    result = await mqtt_switch_control(device_name, state)
    status = result.get("status")
    spoken_name = SMART_HOME_SPOKEN_NAMES.get(device_name, device_name)

    if status in _SUCCESS_STATUSES:
        success_text = (
            f"Ukljucio sam {spoken_name}." if state == "ON"
            else f"Ugasio sam {spoken_name}."
        )
        return resolve_voice_smart_home_response(success_text, response_mode)
    if status in _ALREADY_STATUSES:
        # Truthful and useful: if it is NOT already on, this sentence is
        # visibly wrong and sends someone looking — which is exactly what
        # should have happened three days ago. Spoken in full, because the
        # short form is the same "U redu." as a real success.
        already_text = (
            f"{spoken_name} je već upaljeno." if state == "ON"
            else f"{spoken_name} je već ugašeno."
        )
        return speak_in_full(already_text, response_mode)
    if status == "sent":
        return f"Poslao sam naredbu za {spoken_name}.{_SENT_SUFFIX}"
    if status in ("timeout", "unconfirmed"):
        # Failures are always spoken, regardless of response mode.
        return (
            f"Poslao sam naredbu, ali {spoken_name} nije potvrdio promjenu. "
            "Provjeri je li uređaj dostupan."
        )
    if status == "error":
        return "Ne mogu se spojiti na pametnu kuću. Provjeri MQTT vezu."
    return None
