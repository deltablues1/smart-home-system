"""Source adapters for philosophy corpus ingestion.

All current sources are plain Project Gutenberg texts, so this module
reuses the generic Gutenberg fetcher already hardened in the Christian
ingest pipeline (handles both HTML and plaintext editions, strips
boilerplate, splits into sections) instead of duplicating it.
"""

from __future__ import annotations

from scripts.christian_ingest.adapters import ParsedDocument, fetch_gutenberg

ADAPTERS = {
    "gutenberg": fetch_gutenberg,
}

__all__ = ["ParsedDocument", "ADAPTERS"]
