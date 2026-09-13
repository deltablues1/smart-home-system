"""Which email addresses the house already knows.

The approval gate needs one sync, non-blocking answer: has this address been
dealt with before? A People API call inside a before_tool_callback is neither
sync nor fast, so the answer comes from a small disk-backed set instead.

The set is filled from two directions. Google Contacts seeds it on startup, so
the people already in the address book never trigger a confirmation. And every
address that actually receives mail is remembered, so an address confirmed once
is not asked about again — the gate trains itself down to the case that matters:
a recipient nobody has ever written to, which is exactly the shape of a prompt
injection redirecting mail to an attacker.

Failing closed is deliberate. An unreadable or missing store makes every
address unknown, which costs a confirmation; guessing "known" would cost a
silent send.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_addresses: Optional[Set[str]] = None


def store_path() -> Path:
    return Path(
        os.getenv("KNOWN_RECIPIENTS_FILE", "")
        or Path(__file__).resolve().parents[1] / "data" / "known_recipients.json"
    )


def _normalize(address: str) -> str:
    return (address or "").strip().strip("<>").lower()


def trusted_domains() -> Set[str]:
    raw = os.getenv("APPROVAL_TRUSTED_EMAIL_DOMAINS", "")
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def _load() -> Set[str]:
    global _addresses
    if _addresses is not None:
        return _addresses
    with _lock:
        if _addresses is not None:
            return _addresses
        loaded: Set[str] = set()
        path = store_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                loaded = {_normalize(a) for a in data.get("addresses", []) if a}
        except Exception as exc:
            # Fail closed: an unreadable store means "nothing is known yet".
            logger.warning("Known-recipient store unreadable (%s): %s", path, exc)
        _addresses = loaded
        return _addresses


def _persist() -> None:
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"addresses": sorted(_addresses or set())}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Could not persist known recipients to %s: %s", path, exc)


def is_known(address: str) -> bool:
    """True when this address needs no confirmation. Never blocks on network."""
    normalized = _normalize(address)
    if not normalized or "@" not in normalized:
        return False
    if normalized.rsplit("@", 1)[-1] in trusted_domains():
        return True
    return normalized in _load()


def remember(addresses: Iterable[str]) -> int:
    """Record addresses that have actually been written to."""
    known = _load()
    added = {
        n for n in (_normalize(a) for a in addresses)
        if n and "@" in n and n not in known
    }
    if not added:
        return 0
    with _lock:
        known.update(added)
        _persist()
    logger.info("Learned %d new recipient(s)", len(added))
    return len(added)


def count() -> int:
    return len(_load())


def reset() -> None:
    """Tests only."""
    global _addresses
    _addresses = None


async def refresh_from_contacts(credentials=None, page_size: int = 1000) -> int:
    """Seed the set from Google Contacts. Safe to call at startup and to fail."""
    try:
        from tools.api_implementations.contacts_api import contacts_list_contacts

        if credentials is None:
            from auth.oauth_manager import get_oauth_manager

            credentials = get_oauth_manager().get_credentials()
            # Only vet what we fetched ourselves: at startup the token may not
            # be usable yet, and that is a "try later", not an error. Caller-
            # supplied credentials are the caller's business.
            if credentials is None or not getattr(credentials, "valid", False):
                logger.info("No valid credentials yet; known recipients not seeded")
                return 0

        result = await contacts_list_contacts(credentials, page_size=page_size)
        found = []
        for contact in result.get("contacts", []) or []:
            for field in ("email", "emails"):
                value = contact.get(field)
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str))
        added = remember(found)
        logger.info("Seeded known recipients from Contacts: %d new, %d total", added, count())
        return added
    except Exception as exc:
        # Contacts being unavailable must not stop the app; it only means more
        # confirmations until it works again.
        logger.warning("Could not seed known recipients from Contacts: %s", exc)
        return 0
