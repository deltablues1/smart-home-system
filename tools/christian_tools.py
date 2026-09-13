"""
Christian Knowledge Tools

ADK-compatible tools for accessing a Christian / spiritual knowledge base
via Vertex AI RAG.
"""

import os
import logging

logger = logging.getLogger(__name__)


def get_christian_rag_tool():
    """
    Creates and returns the Vertex AI RAG tool for the Christian knowledge base.
    Returns None if CHRISTIAN_CORPUS_ID is not configured.
    """
    corpus_id = os.getenv("CHRISTIAN_CORPUS_ID")

    if not corpus_id:
        logger.warning(
            "CHRISTIAN_CORPUS_ID not set. christian_guide will work without "
            "RAG knowledge base."
        )
        return None

    try:
        from tools.rag_retrieval_helpers import TranslatingRagRetrieval

        return TranslatingRagRetrieval(
            name="ChristianKnowledgeBase",
            description=(
                "A Christian knowledge base with Scripture, doctrine, spiritual "
                "classics, and guided reflection material. Use this for "
                "Christian theology, prayer, discernment, spiritual exercises, "
                "and reflective dialogue."
            ),
            rag_corpora=[corpus_id],
        )
    except Exception as exc:
        logger.warning(
            "Failed to create Christian RAG tool: %s. christian_guide will "
            "work without RAG.",
            exc,
        )
        return None
