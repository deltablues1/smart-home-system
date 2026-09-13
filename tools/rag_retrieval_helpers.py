"""Vertex AI RAG retrieval with query translation.

Our RAG corpora embed with text-embedding-005. Verified empirically against
the live Christian corpus: an English query correctly retrieves relevant
chunks regardless of the matched document's language (English catechism
queries return catechism/Bible; English queries about the Croatian
encyclical correctly return the encyclical). But a Croatian query almost
never surfaces non-Croatian content, even when it's the only relevant
source — tested with real questions about the Bible/Catechism in Croatian,
0 non-Croatian results turned up even in the top 100 candidates. This is a
known asymmetry in multilingual embedding models (English-pivoted training),
not something chunk size or reranking fixes: an LLM reranker only reorders
whatever the initial vector search already shortlisted, and the shortlist
itself excludes the relevant documents before reranking ever sees them.

Since the primary product is a Croatian-first voice assistant, the fix is
to translate the query to English before hitting Vertex, regardless of the
corpus's actual language mix.

This requires bypassing VertexAiRagRetrieval's default behavior for Gemini 2+
models, which registers RAG as a *native* built-in tool executed server-side
inside Gemini's own tool-use loop — our Python code never sees the query
text in that path, so there's nothing to translate. TranslatingRagRetrieval
forces the regular function-calling path instead, so run_async (ours) always
handles the retrieval call.
"""

from __future__ import annotations

import logging
import os

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.retrieval.vertex_ai_rag_retrieval import VertexAiRagRetrieval
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

TRANSLATION_MODEL = os.getenv("LITE_MODEL", "gemini-3.1-flash-lite")


def _translate_to_english(query: str) -> str:
    try:
        from google import genai

        from tools.google_api_client import get_vertex_ai_config

        config = get_vertex_ai_config()
        client = genai.Client(
            vertexai=True, project=config["project_id"], location="global"
        )
        response = client.models.generate_content(
            model=TRANSLATION_MODEL,
            contents=(
                "Translate the following search query to English. Reply with "
                "ONLY the translated query, no quotes, no explanation. If it "
                "is already in English, repeat it unchanged.\n\n"
                f"Query: {query}"
            ),
        )
        translated = (response.text or "").strip()
        return translated or query
    except Exception as exc:
        logger.warning("RAG query translation failed, using original query: %s", exc)
        return query


class TranslatingRagRetrieval(VertexAiRagRetrieval):
    """VertexAiRagRetrieval that always uses function-call retrieval (never
    Gemini's native built-in RAG path) and translates the query to English
    before hitting Vertex, to correct the corpus's language bias."""

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request) -> None:
        # Skip VertexAiRagRetrieval's native-tool branch for Gemini 2+ models;
        # always register as a plain function-call tool so run_async below
        # actually sees (and can translate) the query text.
        await BaseTool.process_llm_request(
            self, tool_context=tool_context, llm_request=llm_request
        )

    async def run_async(self, *, args: dict, tool_context: ToolContext):
        original_query = args.get("query", "")
        translated_query = _translate_to_english(original_query)
        if translated_query != original_query:
            logger.info(
                "RAG query translated for %s: %r -> %r",
                self.name,
                original_query,
                translated_query,
            )
        return await super().run_async(
            args={"query": translated_query}, tool_context=tool_context
        )
