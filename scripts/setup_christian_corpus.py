"""
Setup Christian Knowledge RAG Corpus.

Creates a Vertex AI RAG corpus for Christian / spiritual sources used by the
christian_guide agent.
"""

import os

import vertexai
from dotenv import load_dotenv
from vertexai.preview import rag

load_dotenv()


def setup_corpus() -> None:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("VERTEX_AI_LOCATION") or os.getenv(
        "GOOGLE_CLOUD_LOCATION",
        "us-west1",
    )

    if not project_id:
        print("Error: GOOGLE_CLOUD_PROJECT not set in .env")
        return

    print("Initializing Vertex AI...")
    print(f"  Project: {project_id}")
    print(f"  Location: {location}")
    vertexai.init(project=project_id, location=location)

    display_name = "Christian Knowledge Base"
    description = (
        "Christian spiritual and doctrinal knowledge base with Scripture, "
        "Catechism references, spiritual classics, discernment texts, and "
        "guided exercises."
    )

    try:
        print(f"\nCreating RAG Corpus: {display_name}...")
        corpus = rag.create_corpus(
            display_name=display_name,
            description=description,
        )
        print("\nSuccess! Christian corpus created.")
        print(f"  Name: {corpus.name}")
        print("\nAdd this to your .env file:")
        print(f"  CHRISTIAN_CORPUS_ID={corpus.name}")
    except Exception as exc:
        print(f"\nCorpus creation failed: {exc}")
        try:
            print("\nSearching for existing corpus...")
            for existing in rag.list_corpora():
                if existing.display_name == display_name:
                    print("Found existing corpus.")
                    print(f"  CHRISTIAN_CORPUS_ID={existing.name}")
                    return
        except Exception as list_exc:
            print(f"Listing existing corpora failed: {list_exc}")


if __name__ == "__main__":
    print("=" * 70)
    print("Christian Knowledge RAG Corpus Setup")
    print("=" * 70)
    setup_corpus()
    print("=" * 70)
