"""Christian corpus registry loading and chunk generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.christian_ingest.adapters import ADAPTERS, ParsedDocument


CHUNK_TARGETS = {
    "scripture": 900,
    "doctrine": 1400,
    "reflection": 2400,
    "exercise": 1000,
    "prayer": 900,
    "encyclical": 1500,
}

CHUNK_OVERLAP = {
    "scripture": 120,
    "doctrine": 180,
    "reflection": 250,
    "exercise": 120,
    "prayer": 120,
    "encyclical": 200,
}

# Characters-per-token estimate shared with build_chunk_records' token_estimate,
# used to translate our char-based local targets into Vertex RAG Engine's
# token-based TransformationConfig so the two stay in sync.
CHARS_PER_TOKEN = 4


def vertex_chunk_config(text_type: str) -> tuple[int, int]:
    """Token-based (chunk_size, chunk_overlap) for Vertex upload, derived
    from the same per-text_type targets used for local chunk_records."""
    target_chars = CHUNK_TARGETS.get(text_type, 2000)
    overlap_chars = CHUNK_OVERLAP.get(text_type, 200)
    chunk_size = max(100, target_chars // CHARS_PER_TOKEN)
    chunk_overlap = max(0, min(overlap_chars // CHARS_PER_TOKEN, chunk_size - 1))
    return chunk_size, chunk_overlap


def load_registry(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("Registry root must be a JSON array")
    return data


def iter_enabled_sources(
    registry: list[dict[str, Any]],
    source_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    results = []
    for entry in registry:
        if not entry.get("enabled", True):
            continue
        if source_ids and entry["source_id"] not in source_ids:
            continue
        results.append(entry)
    return results


def fetch_source_documents(entry: dict[str, Any]) -> list[ParsedDocument]:
    adapter_name = entry["adapter"]
    if adapter_name not in ADAPTERS:
        raise ValueError(f"Unknown adapter: {adapter_name}")
    return ADAPTERS[adapter_name](entry)


def chunk_text(text: str, target: int, overlap: int) -> list[str]:
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + target, text_len)
        if end < text_len:
            split_candidates = [
                text.rfind("\n\n", start, end),
                text.rfind(". ", start, end),
                text.rfind("\n", start, end),
            ]
            split_at = max(split_candidates)
            if split_at > start + (target // 2):
                end = split_at + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_len:
            break
        start = max(end - overlap, start + 1)

    return chunks


def build_chunk_records(
    entry: dict[str, Any],
    documents: list[ParsedDocument],
) -> list[dict[str, Any]]:
    text_type = entry.get("text_type", "reflection")
    target = CHUNK_TARGETS.get(text_type, 2000)
    overlap = CHUNK_OVERLAP.get(text_type, 200)

    records: list[dict[str, Any]] = []
    for document in documents:
        # Scripture documents already arrive as small verse-range units.
        # Keep them intact so retrieval/citation aligns with verse metadata.
        if {
            "book",
            "chapter",
            "verse_start",
            "verse_end",
        }.issubset(document.hierarchy):
            chunks = [document.clean_text]
        else:
            chunks = chunk_text(document.clean_text, target=target, overlap=overlap)
        total = len(chunks)
        for idx, chunk in enumerate(chunks):
            records.append(
                {
                    "source_id": entry["source_id"],
                    "work_title": entry["work_title"],
                    "author": entry["author"],
                    "source_family": entry["source_family"],
                    "canonical_url": entry["canonical_url"],
                    "rights_class": entry["rights_class"],
                    "language": entry["language"],
                    "text_type": entry["text_type"],
                    "authority_level": entry["authority_level"],
                    "citation_short": entry["citation_style"],
                    "persona_affinity": entry.get("persona_affinity", []),
                    "document_id": document.document_id,
                    "document_title": document.title,
                    "hierarchy": document.hierarchy,
                    "chunk_index": idx,
                    "chunk_total": total,
                    "clean_text": chunk,
                    "token_estimate": max(1, len(chunk) // 4),
                    "query_intents": infer_query_intents(entry),
                }
            )
    return records


def infer_query_intents(entry: dict[str, Any]) -> list[str]:
    text_type = entry.get("text_type")
    authority = entry.get("authority_level")

    intents = {"teach"}
    if text_type in {"scripture", "reflection", "exercise", "prayer"}:
        intents.add("reflect")
    if text_type in {"exercise", "prayer"}:
        intents.add("guide")
    if text_type == "scripture":
        intents.add("discern")
    if authority in {"medium", "devotional"}:
        intents.add("discern")
    return sorted(intents)
