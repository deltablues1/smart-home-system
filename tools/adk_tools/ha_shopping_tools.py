"""Home Assistant shopping list, as spoken to rather than typed into.

The list lives in HA as a `todo` entity, so every operation goes through
todo.add_item / update_item / remove_item, and reading it needs get_items with
`?return_response`.

Three things shape the design, all learned elsewhere in this system:

* **Say what happened, not what was sent.** Every write re-reads the list and
  reports the state that actually resulted. "Dodao sam ulje" is only true if
  ulje is on the list afterwards.
* **Voice text is approximate.** Matching folds diacritics and case, then tries
  exact, prefix and substring in that order. What it will not do is guess
  between two candidates: marking the wrong thing bought means the user comes
  home without it, so an ambiguous name is reported, never resolved by picking.
* **Blocking HTTP belongs in a thread.** Registration wraps every tool with
  _offload, keyed on the list as one device, so two edits cannot interleave a
  read-modify-write.
"""

import logging
import os
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from tools.adk_tools import _offload
from tools.adk_tools.ha_adk_tools import _ha_request

logger = logging.getLogger(__name__)

OPEN = "needs_action"
DONE = "completed"


def _entity() -> str:
    return os.getenv("HA_SHOPPING_LIST_ENTITY", "todo.shopping_list").strip()


def _fold(text: str) -> str:
    """Lowercase and strip diacritics, so "brasno" and the real spelling match."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower().strip())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _split(raw: str) -> List[str]:
    """Split a comma-separated list of items.

    Only on "," and ";" - deliberately not on " i ", which would tear "sol i
    papar" in half. The agent is told to hand spoken lists over already
    comma-separated, which is a thing models are good at.
    """
    parts = []
    for chunk in (raw or "").replace(";", ",").split(","):
        name = chunk.strip(" .!?")
        if name:
            parts.append(name)
    return parts


def _read(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Current items. `status` filters to open or completed ones."""
    payload: Dict[str, Any] = {"entity_id": _entity()}
    if status:
        payload["status"] = status
    body = _ha_request("/api/services/todo/get_items?return_response", payload) or {}
    response = (body.get("service_response") or {}).get(_entity()) or {}
    return list(response.get("items") or [])


def _match(name: str, items: List[Dict[str, Any]]) -> Tuple[Optional[str], List[str]]:
    """Resolve a spoken name to one item on the list.

    Returns (summary, candidates). A single candidate resolves; several are
    handed back unresolved. Choosing for the user is the one thing this must
    not do - the cost of marking the wrong item bought is paid at the shop.
    """
    target = _fold(name)
    if not target:
        return None, []

    exact = [i["summary"] for i in items if _fold(i.get("summary", "")) == target]
    if len(exact) == 1:
        return exact[0], exact
    if exact:
        return None, exact

    for test in (
        lambda s: _fold(s).startswith(target),
        lambda s: target in _fold(s),
    ):
        hits = [i["summary"] for i in items if test(i.get("summary", ""))]
        if len(hits) == 1:
            return hits[0], hits
        if hits:
            return None, hits
    return None, []


def _state(note: str = "") -> Dict[str, Any]:
    """The list as it stands now, for the agent to read back."""
    items = _read()
    result: Dict[str, Any] = {
        "status": "ok",
        "za_kupiti": [i["summary"] for i in items if i.get("status") == OPEN],
        "kupljeno": [i["summary"] for i in items if i.get("status") == DONE],
    }
    result["broj_za_kupiti"] = len(result["za_kupiti"])
    if note:
        result["napomena"] = note
    return result


def shopping_list_show() -> dict:
    """Prikazi sto je trenutno na listi za kupovinu.

    Vraca `za_kupiti` (jos treba kupiti) i `kupljeno` (vec oznaceno).
    """
    return _state()


def shopping_list_add(items: str) -> dict:
    """Dodaj stavke na listu za kupovinu.

    Args:
        items: stavke odvojene ZAREZOM, npr. "ulje, brasno, mlijeko".
               Izgovorenu listu prvo pretvori u oblik sa zarezima.

    Ne dodaje ono sto je vec na listi - ponovno izgovaranje iste stavke ne
    smije napraviti duplikat.
    """
    names = _split(items)
    if not names:
        return {"status": "error", "error": "Nisi rekao sto da dodam."}

    open_now = [i for i in _read() if i.get("status") == OPEN]
    added, already = [], []
    for name in names:
        matched, _ = _match(name, open_now)
        if matched:
            already.append(matched)
            continue
        _ha_request(
            "/api/services/todo/add_item",
            {"entity_id": _entity(), "item": name},
        )
        added.append(name)

    result = _state()
    result["dodano"] = added
    if already:
        result["vec_na_listi"] = already
    return result


def _set_status(items: str, status: str) -> dict:
    names = _split(items)
    if not names:
        return {"status": "error", "error": "Nisi rekao koje stavke."}

    current = _read()
    changed, missing, ambiguous = [], [], {}
    for name in names:
        matched, candidates = _match(name, current)
        if matched is None:
            if candidates:
                ambiguous[name] = candidates
            else:
                missing.append(name)
            continue
        _ha_request(
            "/api/services/todo/update_item",
            {"entity_id": _entity(), "item": matched, "status": status},
        )
        changed.append(matched)

    result = _state()
    result["promijenjeno"] = changed
    if missing:
        result["nije_pronadeno"] = missing
    if ambiguous:
        result["vise_kandidata"] = ambiguous
    return result


def shopping_list_complete(items: str) -> dict:
    """Oznaci stavke kao kupljene.

    Args:
        items: stavke odvojene zarezom, npr. "ulje, brasno".

    Stavku koju ne nade NE pogada: vrati je u `nije_pronadeno` ili
    `vise_kandidata` da je mozes pitati korisnika.
    """
    return _set_status(items, DONE)


def shopping_list_uncomplete(items: str) -> dict:
    """Vrati stavke medu one koje treba kupiti ("ipak nisam kupio").

    Args:
        items: stavke odvojene zarezom.
    """
    return _set_status(items, OPEN)


def shopping_list_remove(items: str) -> dict:
    """Obrisi stavke s liste (nisu kupljene, samo ih vise ne trebas).

    Args:
        items: stavke odvojene zarezom.
    """
    names = _split(items)
    if not names:
        return {"status": "error", "error": "Nisi rekao sto da maknem."}

    current = _read()
    removed, missing, ambiguous = [], [], {}
    for name in names:
        matched, candidates = _match(name, current)
        if matched is None:
            if candidates:
                ambiguous[name] = candidates
            else:
                missing.append(name)
            continue
        _ha_request(
            "/api/services/todo/remove_item",
            {"entity_id": _entity(), "item": matched},
        )
        removed.append(matched)

    result = _state()
    result["maknuto"] = removed
    if missing:
        result["nije_pronadeno"] = missing
    if ambiguous:
        result["vise_kandidata"] = ambiguous
    return result


def shopping_list_complete_all_except(keep: str) -> dict:
    """Oznaci SVE kao kupljeno osim navedenih stavki.

    Za "kupio sam sve osim mlijeka i kruha".

    Args:
        keep: stavke koje NISI kupio, odvojene zarezom.

    Ako neku od navedenih iznimaka ne moze jednoznacno prepoznati, NE mijenja
    nista i vraca pitanje. Oznaciti cijelu listu kupljenom i promasiti iznimku
    znaci da korisnik dode kuci bez toga - pa se u toj situaciji radije pita.
    """
    names = _split(keep)
    if not names:
        return {
            "status": "error",
            "error": "Nisi rekao sto ostaje na listi.",
        }

    open_items = [i for i in _read() if i.get("status") == OPEN]
    if not open_items:
        return _state("Lista je vec prazna, nema sto oznaciti.")

    keep_summaries, missing, ambiguous = [], [], {}
    for name in names:
        matched, candidates = _match(name, open_items)
        if matched is None:
            if candidates:
                ambiguous[name] = candidates
            else:
                missing.append(name)
            continue
        keep_summaries.append(matched)

    if ambiguous or missing:
        result = _state("Nista nisam promijenio.")
        result["status"] = "needs_clarification"
        if missing:
            result["nije_pronadeno"] = missing
        if ambiguous:
            result["vise_kandidata"] = ambiguous
        return result

    keep_folded = {_fold(s) for s in keep_summaries}
    completed = []
    for item in open_items:
        summary = item.get("summary", "")
        if _fold(summary) in keep_folded:
            continue
        _ha_request(
            "/api/services/todo/update_item",
            {"entity_id": _entity(), "item": summary, "status": DONE},
        )
        completed.append(summary)

    result = _state()
    result["oznaceno_kupljeno"] = completed
    result["ostaje"] = keep_summaries
    return result


def shopping_list_clear_completed() -> dict:
    """Makni s liste sve sto je vec oznaceno kao kupljeno."""
    _ha_request("/api/services/todo/remove_completed_items", {"entity_id": _entity()})
    return _state()


def get_shopping_list_tools() -> list:
    """Shopping list tools, offloaded like every other HA tool.

    One device key for the whole list: each of these is a read-modify-write,
    and two of them interleaving would decide what to change from a list the
    other one has already moved.
    """
    tools = [
        shopping_list_show,
        shopping_list_add,
        shopping_list_complete,
        shopping_list_uncomplete,
        shopping_list_remove,
        shopping_list_complete_all_except,
        shopping_list_clear_completed,
    ]
    wrap = _offload.offload(
        device="shopping_list",
        deadline_env="SHOPPING_LIST_DEADLINE_SECONDS",
        default_deadline=25.0,
    )
    return [wrap(fn) for fn in tools]
