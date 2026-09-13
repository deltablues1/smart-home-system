"""
Ingest philosophy corpus sources from a JSON source registry.

This script:
1. Loads data/philosophy/source_registry.json
2. Fetches and normalizes the configured sources
3. Emits chunked JSONL records with metadata
4. Optionally uploads normalized source documents into Vertex AI RAG

Supersedes the older ad-hoc scripts/ingest_philosophy.py (single Republic
fetch + an unrelated wikitext sample) with the same structured
registry/adapter/chunking pattern used for the Christian corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.append(str(Path(__file__).resolve().parent.parent))

from scripts.philosophy_ingest.pipeline import (  # noqa: E402
    build_chunk_records,
    fetch_source_documents,
    iter_enabled_sources,
    load_registry,
    vertex_chunk_config,
)


DEFAULT_REGISTRY = Path("data/philosophy/source_registry.json")
DEFAULT_OUTPUT_DIR = Path("data/philosophy/build")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest philosophy corpus sources")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="Path to source registry JSON",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for normalized output files",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        help="Limit ingestion to one or more source_id values",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload normalized source documents to PHILOSOPHY_CORPUS_ID",
    )
    return parser.parse_args()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_upload_documents(
    source: dict,
    documents: list,
) -> list[dict[str, str]]:
    return [
        {
            "text": document.clean_text,
            "title": f"{source['source_id']}__{document.title}",
        }
        for document in documents
    ]


def main() -> None:
    load_dotenv()
    args = parse_args()

    registry = load_registry(args.registry)
    source_ids = set(args.source_ids or [])
    sources = iter_enabled_sources(registry, source_ids=source_ids or None)

    if not sources:
        print("No matching sources to ingest.")
        return

    corpus_name = None
    if args.upload:
        import os
        from tools.ingestion_tools import upload_many_to_rag_corpus

        corpus_name = os.getenv("PHILOSOPHY_CORPUS_ID")
        if not corpus_name:
            raise SystemExit(
                "PHILOSOPHY_CORPUS_ID is required when using --upload"
            )
    else:
        upload_many_to_rag_corpus = None

    summary: list[dict] = []
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    for source in sources:
        print(f"\n=== {source['display_name']} ({source['source_id']}) ===")
        documents = fetch_source_documents(source)
        if not documents:
            print("No documents parsed; skipping.")
            continue

        chunk_records = build_chunk_records(source, documents)
        source_dir = output_dir / source["source_id"]

        write_json(source_dir / "source.json", source)
        write_json(
            source_dir / "documents.json",
            [
                {
                    "document_id": doc.document_id,
                    "title": doc.title,
                    "hierarchy": doc.hierarchy,
                    "char_count": len(doc.clean_text),
                }
                for doc in documents
            ],
        )
        write_jsonl(source_dir / "chunks.jsonl", chunk_records)

        if args.upload and corpus_name:
            chunk_size, chunk_overlap = vertex_chunk_config(
                source.get("text_type", "treatise")
            )
            upload_summary = upload_many_to_rag_corpus(
                build_upload_documents(source, documents),
                corpus_name=corpus_name,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            print(
                "Uploaded "
                f"{upload_summary['documents']} documents in "
                f"{upload_summary['batches']} batches."
            )

        summary.append(
            {
                "source_id": source["source_id"],
                "documents": len(documents),
                "chunks": len(chunk_records),
                "output_dir": str(source_dir),
            }
        )
        print(
            f"Parsed {len(documents)} documents and wrote {len(chunk_records)} chunks."
        )

    write_json(output_dir / "summary.json", summary)
    print(f"\nDone. Summary written to {output_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
