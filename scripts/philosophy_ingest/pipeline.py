"""Philosophy corpus registry loading and chunk generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.philosophy_ingest.adapters import ADAPTERS, ParsedDocument

# Character targets per text_type. Dialogues/treatises keep larger chunks so a
# full Socratic exchange or argument step survives intact; meditations and the
# manual are terse and aphoristic, so smaller chunks keep each entry distinct.
CHUNK_TARGETS = {
    "dialogue": 1800,
    "treatise": 1800,
    "doxography": 1600,
    "meditation": 1000,
    "manual": 900,
}

CHUNK_OVERLAP = {
    "dialogue": 200,
    "treatise": 200,
    "doxography": 180,
    "meditation": 120,
    "manual": 100,
}

# Characters-per-token estimate, matched to build_chunk_records' token_estimate,
# used to translate char-based local targets into Vertex RAG Engine's
# token-based TransformationConfig.
CHARS_PER_TOKEN = 4


def vertex_chunk_config(text_type: str) -> tuple[int, int]:
    """Token-based (chunk_size, chunk_overlap) for Vertex upload, derived
    from the same per-text_type targets used for local chunk_records."""
    target_chars = CHUNK_TARGETS.get(text_type, 1800)
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
    text_type = entry.get("text_type", "treatise")
    target = CHUNK_TARGETS.get(text_type, 1800)
    overlap = CHUNK_OVERLAP.get(text_type, 200)

    records: list[dict[str, Any]] = []
    for document in documents:
        chunks = chunk_text(document.clean_text, target=target, overlap=overlap)
        total = len(chunks)
        for idx, chunk in enumerate(chunks):
            records.append(
                {
                    "source_id": entry["source_id"],
                    "work_title": entry["work_title"],
                    "author": entry["author"],
                    "translator": entry.get("translator"),
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

    intents = {"teach"}
    if text_type in {"dialogue", "meditation"}:
        intents.add("reflect")
    if text_type == "dialogue":
        intents.add("question")
    if text_type in {"manual", "meditation"}:
        intents.add("guide")
    return sorted(intents)
