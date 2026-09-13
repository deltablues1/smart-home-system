"""
Google Search API Implementation

Implements two search strategies:
1. Vertex AI Grounding (primary) - uses Gemini's built-in search grounding
2. Google Custom Search API (fallback) - uses Programmable Search Engine

Automatic fallback: If Vertex AI returns 429 (quota exceeded), falls back to Custom Search.
"""

import logging
import os
import asyncio
from typing import Dict, Any, Optional, List
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Custom Search API configuration.
#
# Read per call, not at import: these were module constants, so adding the key
# to .env did nothing until the process restarted — and the failure is silent,
# a fallback to grounding rather than an error.
#
# GOOGLE_CUSTOM_SEARCH_API_KEY first, because the key that works here is a
# Google Cloud key with the Custom Search API enabled on the project; the
# Gemini/AI-Studio key in GOOGLE_API_KEY is usually not that key and comes
# back as "API keys are not supported by this API".
def _custom_search_api_key() -> Optional[str]:
    return (
        os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or None
    )


def _custom_search_cx() -> Optional[str]:
    return os.getenv("GOOGLE_CUSTOM_SEARCH_CX") or None

# Croatian queries need gl/hl=hr. Without them Custom Search answers from the
# caller's IP locale, so a question about prices in Croatia comes back with
# international shops the user cannot buy from.
_CROATIAN_HINTS = frozenset({
    "cijena", "cijene", "cijenu", "kosta", "kostaju", "koliko", "kolika",
    "najbolji", "najbolja", "kupiti", "kupovina", "gdje", "kada", "kako",
    "zasto", "hrvatska", "hrvatskoj", "hrvatske", "ponuda", "akcija",
})


def _grounding_system_instruction(query: str) -> str:
    """Rules the grounding model gets before it searches.

    Without this it is handed a bare "answer this" and has no idea what day it
    is, which country the asker is in, or that a price is worthless without the
    shop and the date it was read.
    """
    try:
        import pytz
        from datetime import datetime

        tz_name = os.getenv("USER_TIMEZONE", "Europe/Zagreb")
        today = datetime.now(pytz.timezone(tz_name)).strftime("%Y-%m-%d")
    except Exception:  # pragma: no cover - clock/tz problems must not block search
        today = "unknown"

    croatian = _looks_croatian(query)
    lines = [
        f"Today is {today}. Treat it as the present when judging how fresh a source is.",
        "Answer in the same language as the question.",
        "Every number, price or date you report must carry its source and the date "
        "that source was published or last updated. If you cannot establish the date, "
        "say the figure is undated rather than dropping the caveat.",
        "Prices must include the currency and say whether tax is included. Never "
        "convert or round a price you did not read directly in a source.",
        "State plainly what you could not find. Do not fill gaps from memory.",
    ]
    if croatian:
        lines.insert(
            2,
            "The question is Croatian: prefer Croatian sources and Croatian shops, "
            "and report prices in EUR as listed there.",
        )
    return " ".join(lines)


def _looks_croatian(query: str) -> bool:
    """Best-effort language sniff for the Custom Search locale parameters."""
    lowered = query.lower()
    if any(ch in lowered for ch in "čćžšđ"):
        return True
    words = {w.strip(".,?!\"'()[]") for w in lowered.split()}
    return bool(words & _CROATIAN_HINTS)


async def google_search_grounding(
    credentials,
    query: str,
    max_results: int = 5,
    include_citations: bool = True
) -> Dict[str, Any]:
    """
    Perform web search using Google's Vertex AI Grounding with Search

    This uses Google's built-in grounding feature which:
    - Searches Google's index
    - Returns relevant snippets
    - Automatically includes source citations
    - No separate API key needed (uses Vertex AI credentials)

    Args:
        credentials: Google Cloud credentials (not used - Vertex AI handles auth)
        query: Search query
        max_results: Maximum number of results to return (default: 5)
        include_citations: Whether to include source citations (default: True)

    Returns:
        Dictionary with:
        - query: Original query
        - results: List of search results with snippets and URLs
        - grounding_metadata: Attribution and source information

    Reference:
        https://ai.google.dev/gemini-api/docs/grounding
        https://google.github.io/adk-docs/grounding/google_search_grounding/
    """
    try:
        logger.info(f"Executing Google Search Grounding: {query}")

        # Configure Vertex AI client.
        # Grounding with gemini-3.x must use the GLOBAL endpoint (the regional
        # VERTEX_AI_LOCATION like us-west1 only exposes 2.5 and returns 404 for
        # the 3.x preview models), so force global here rather than the regional
        # location from get_vertex_ai_config().
        import os
        from tools.google_api_client import get_vertex_ai_config
        config = get_vertex_ai_config()
        grounding_location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
        grounding_model = (
            os.getenv("GROUNDING_MODEL", os.getenv("FLASH_MODEL", "gemini-3.5-flash")).strip()
            or "gemini-3.5-flash"
        )

        client = genai.Client(
            vertexai=True,
            project=config.get("project_id"),
            location=grounding_location,
        )

        # Create grounding tool configuration
        # This tells Gemini to use Google Search for grounding
        google_search_tool = types.Tool(
            google_search=types.GoogleSearch()
        )

        # Generate content with grounding
        # The model will automatically search and cite sources.
        #
        # In a THREAD, because client.models.generate_content is the genai
        # SDK's synchronous surface: called bare inside this async function it
        # holds the single event loop for the whole request, and a grounding
        # call here takes 60-90 seconds. Measured 2026-09-06 on the Pi, three
        # of them back to back froze the entire web process for 2.5 minutes --
        # "upali svjetlo u hodniku" arrived at 17:16:10 and the MQTT publish
        # went out at 17:18:41, the moment the last grounding call returned.
        # A house that stops answering its light switches because someone
        # asked a research question is the user-visible half of this.
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=grounding_model,
            contents=query,
            config=types.GenerateContentConfig(
                tools=[google_search_tool],
                response_modalities=["TEXT"],
                system_instruction=_grounding_system_instruction(query),
            ),
        )

        # Extract search results and grounding metadata
        result_text = response.text if hasattr(response, 'text') else ""

        # Extract grounding metadata (citations)
        grounding_metadata = {}
        if hasattr(response, 'candidates') and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, 'grounding_metadata'):
                grounding_metadata = {
                    'grounding_support': candidate.grounding_metadata
                }

        # Parse grounding chunks to extract source URLs
        sources = []
        if grounding_metadata and hasattr(grounding_metadata.get('grounding_support'), 'grounding_chunks'):
            for chunk in grounding_metadata['grounding_support'].grounding_chunks:
                if hasattr(chunk, 'web'):
                    sources.append({
                        'url': chunk.web.uri,
                        'title': chunk.web.title if hasattr(chunk.web, 'title') else None
                    })

        result = {
            "query": query,
            "answer": result_text,
            "sources": sources,
            "source_count": len(sources),
            "grounding_metadata": str(grounding_metadata) if grounding_metadata else None
        }

        logger.info(f"Google Search completed: {len(sources)} sources found")
        return result

    except Exception as e:
        error_str = str(e)
        logger.error(f"Google Search Grounding failed: {e}")

        # Check if this is a quota error (429) - trigger fallback
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
            logger.info("Vertex AI quota exhausted, attempting Custom Search fallback...")
            fallback_result = await google_custom_search(
                query=query,
                num_results=max_results
            )
            if fallback_result and not fallback_result.get("error"):
                return fallback_result

        return {
            "query": query,
            "error": str(e),
            "answer": None,
            "sources": [],
            "source_count": 0
        }


async def google_custom_search(
    query: str,
    num_results: int = 10,
    locale: Optional[str] = None
) -> Dict[str, Any]:
    """
    Perform web search using Google Custom Search API (Programmable Search Engine)

    This is a reliable fallback when Vertex AI Grounding hits quota limits.

    Requirements:
        - GOOGLE_API_KEY environment variable
        - GOOGLE_CUSTOM_SEARCH_CX environment variable (Search Engine ID)

    Get your Search Engine ID from: https://programmablesearchengine.google.com/

    Args:
        query: Search query
        num_results: Number of results (max 10 per request)
        locale: Two-letter country/language code for gl+hl. None auto-detects
            Croatian and leaves other queries on the API default.

    Returns:
        Dictionary with search results
    """
    try:
        api_key = _custom_search_api_key()
        engine_id = _custom_search_cx()

        if not api_key:
            return {
                "query": query,
                "error": (
                    "Custom Search key not configured. Set GOOGLE_CUSTOM_SEARCH_API_KEY "
                    "to a Google Cloud API key with the Custom Search API enabled on "
                    "the project (a Gemini/AI Studio key is rejected by this API)."
                ),
                "sources": [],
                "source_count": 0
            }

        if not engine_id:
            return {
                "query": query,
                "error": "GOOGLE_CUSTOM_SEARCH_CX not configured. Create a Custom Search Engine at https://programmablesearchengine.google.com/",
                "sources": [],
                "source_count": 0
            }

        import aiohttp

        logger.info(f"Executing Google Custom Search: {query}")

        # Build the Custom Search API URL
        base_url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": api_key,
            "cx": engine_id,
            "q": query,
            "num": min(num_results, 10)  # API max is 10
        }

        if locale is None and _looks_croatian(query):
            locale = "hr"
        if locale:
            params["gl"] = locale
            params["hl"] = locale

        async with aiohttp.ClientSession() as session:
            async with session.get(base_url, params=params) as response:
                if response.status == 429:
                    logger.error("Custom Search API quota also exhausted")
                    return {
                        "query": query,
                        "error": "Both Vertex AI and Custom Search API quotas exhausted. Try again later.",
                        "sources": [],
                        "source_count": 0
                    }

                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Custom Search API error: {response.status} - {error_text}")
                    return {
                        "query": query,
                        "error": f"Custom Search API error: {response.status}",
                        "sources": [],
                        "source_count": 0
                    }

                data = await response.json()

        # Parse results
        sources = []
        items = data.get("items", [])

        for item in items:
            sources.append({
                "url": item.get("link"),
                "title": item.get("title"),
                "snippet": item.get("snippet")
            })

        # Build a summary answer from snippets
        answer_parts = [f"Search results for '{query}':\n"]
        for i, source in enumerate(sources[:5], 1):
            answer_parts.append(f"{i}. {source['title']}")
            if source.get('snippet'):
                answer_parts.append(f"   {source['snippet']}")
            answer_parts.append(f"   URL: {source['url']}\n")

        result = {
            "query": query,
            "answer": "\n".join(answer_parts),
            "sources": sources,
            "source_count": len(sources),
            "search_method": "custom_search_api"
        }

        logger.info(f"Custom Search completed: {len(sources)} results found")
        return result

    except ImportError:
        logger.error("aiohttp not installed. Run: pip install aiohttp")
        return {
            "query": query,
            "error": "aiohttp library required. Run: pip install aiohttp",
            "sources": [],
            "source_count": 0
        }
    except Exception as e:
        logger.error(f"Custom Search failed: {e}")
        return {
            "query": query,
            "error": str(e),
            "sources": [],
            "source_count": 0
        }


def _raw_search_providers():
    """Providers that return a list of pages, best first.

    Custom Search when it is configured, Jina when there is a key, and
    DuckDuckGo — which needs neither — as the one that always answers. Anything
    unconfigured reports an error and the chain moves on, so adding a key later
    is enough to promote a provider without touching code.
    """
    from tools.api_implementations.search_providers import (
        duckduckgo_search,
        jina_search,
    )

    async def _custom(query, num_results, locale):
        return await google_custom_search(query, num_results=num_results, locale=locale)

    return [
        ("custom_search_api", _custom),
        ("jina_search", jina_search),
        ("duckduckgo_search", duckduckgo_search),
    ]


async def google_search_simple(
    credentials,
    query: str,
    num_results: int = 10
) -> Dict[str, Any]:
    """
    Web search returning raw results: one title, URL and snippet per hit.

    Goes straight to the Custom Search API. It deliberately does NOT call
    google_search_grounding: grounding returns a single AI-written summary, and
    a researcher handed a summary here has nothing left to open and read. Use
    google_search_grounding for a quick overview, this for sources to scrape.

    Falls back to grounding only when Custom Search is not configured, and says
    so in `search_method` and `warning` rather than pretending these are raw hits.

    Args:
        credentials: Google Cloud credentials (not used, kept for API consistency)
        query: Search query
        num_results: Number of results to return (max 10 per request)

    Returns:
        Dictionary with `results`: [{"url", "title", "snippet"}, ...]
    """
    try:
        logger.info(f"Executing simple Google Search: {query}")

        locale = "hr" if _looks_croatian(query) else None
        tried = []

        for name, call in _raw_search_providers():
            result = await call(query, num_results, locale)
            error = result.get("error")
            if not error and result.get("sources"):
                return {
                    "query": query,
                    "results": result.get("sources", []),
                    "result_count": result.get("source_count", 0),
                    "search_method": result.get("search_method", name),
                }
            tried.append(f"{name}: {error or 'no results'}")
            logger.info("Raw search provider %s unavailable (%s)", name, error or "no results")

        # Every raw provider is out. Grounding still answers, but its "sources"
        # are citations behind a summary, not a result list — say so rather than
        # letting the researcher treat them as pages it has read.
        logger.warning("No raw search provider worked (%s); falling back to grounding", "; ".join(tried))
        grounded = await google_search_grounding(
            credentials=credentials,
            query=query,
            max_results=num_results
        )

        return {
            "query": query,
            "results": grounded.get("sources", []),
            "result_count": grounded.get("source_count", 0),
            "search_method": "vertex_ai_grounding_fallback",
            "providers_tried": tried,
            "warning": (
                "No raw search provider was available; these are grounding "
                "citations, not search results. Open the pages before quoting "
                "anything from them."
            )
        }

    except Exception as e:
        logger.error(f"Simple Google Search failed: {e}")
        return {
            "query": query,
            "error": str(e),
            "results": [],
            "result_count": 0
        }


def register_google_search_tools(tool_registry) -> None:
    """
    Register Google Search tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # Register grounding-based search
    tool_registry.register_tool(
        name="google_search_grounding",
        function=google_search_grounding,
        description="Search the web using Google's Vertex AI Grounding. Returns AI-generated answer with cited sources.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5
                },
                "include_citations": {
                    "type": "boolean",
                    "description": "Include source citations (default: true)",
                    "default": True
                }
            },
            "required": ["query"]
        },
        requires_auth=False,  # Uses Vertex AI credentials automatically
        auth_type="vertex_ai"
    )

    # Register simple search
    tool_registry.register_tool(
        name="google_search_simple",
        function=google_search_simple,
        description="Web search returning raw results (title, URL, snippet) via Google Custom Search. Use this to find pages to open; use google_search_grounding for a quick AI overview.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        },
        requires_auth=False,
        auth_type="vertex_ai"
    )

    # Register Custom Search API (direct, reliable)
    tool_registry.register_tool(
        name="google_custom_search",
        function=google_custom_search,
        description="Search the web using Google Custom Search API (Programmable Search Engine). More reliable than Grounding, doesn't hit Vertex AI quotas.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results (max: 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        },
        requires_auth=False,
        auth_type="api_key"
    )

    # Log configuration status
    cx = _custom_search_cx()
    if cx and _custom_search_api_key():
        logger.info(f"Custom Search API configured (cx: {cx[:8]}...)")
    elif cx:
        logger.warning(
            "GOOGLE_CUSTOM_SEARCH_CX is set but no Custom Search API key is — "
            "google_search_simple will fall back to grounding citations"
        )
    else:
        logger.warning("GOOGLE_CUSTOM_SEARCH_CX not set - Custom Search unavailable")

    logger.info("Google Search tools registered successfully")
