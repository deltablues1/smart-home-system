"""
Research ADK Tools

ADK-compatible wrappers for Research tools:
- Google Search Grounding (Vertex AI)
- YouTube Transcript extraction
- Web Scraping (BeautifulSoup)

All tools are FREE and require NO external API keys!
Uses Vertex AI credentials from environment.
"""

import logging
import os
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


async def google_search_grounding(
    query: str,
    max_results: int = 5,
    include_citations: bool = True
) -> dict:
    """
    Search the web using Google's Vertex AI Grounding with automatic citations.

    This uses Google's built-in grounding feature which searches Google's index,
    returns relevant snippets, and automatically includes source citations.
    No separate API key needed - uses Vertex AI credentials from environment.

    Best for: fact-checking, recent information, news, research questions.

    Args:
        query: The search query
        max_results: Maximum number of results (default: 5)
        include_citations: Include source citations (default: True)

    Returns:
        Dictionary with search results, answer summary, and cited sources
    """
    try:
        from tools.api_implementations.google_search_api import google_search_grounding as google_search_impl

        result = await google_search_impl(
            credentials=None,  # Vertex AI uses env credentials
            query=query,
            max_results=max_results,
            include_citations=include_citations
        )
        return result
    except Exception as e:
        logger.error(f"Google search grounding failed: {e}")
        return {"error": str(e), "query": query}


async def google_search_simple(
    query: str,
    num_results: int = 10
) -> dict:
    """
    Simple web search that returns URLs and snippets without AI summarization.

    Best for: finding URLs to scrape, discovering resources, getting raw search results.

    Args:
        query: The search query
        num_results: Number of results to return (default: 10)

    Returns:
        Dictionary with list of search results (URLs, titles, snippets)
    """
    try:
        from tools.api_implementations.google_search_api import google_search_simple as google_search_simple_impl

        result = await google_search_simple_impl(
            credentials=None,
            query=query,
            num_results=num_results
        )
        return result
    except Exception as e:
        logger.error(f"Simple Google search failed: {e}")
        return {"error": str(e), "query": query}


async def youtube_get_transcript(
    url: str,
    languages: Optional[List[str]] = None,
    preserve_formatting: bool = False
) -> dict:
    """
    Get transcript (captions/subtitles) from a YouTube video.

    Works with Croatian and English videos. Extracts full transcript text
    for analysis, summarization, or information extraction.

    Best for: analyzing video content, extracting information from talks/presentations,
    creating summaries of educational videos.

    Args:
        url: YouTube video URL or video ID
        languages: Preferred languages for transcript (default: ['hr', 'en'])
        preserve_formatting: Keep timestamps and segment structure (default: False)

    Returns:
        Dictionary with video transcript, title, and metadata
    """
    try:
        from tools.api_implementations.youtube_api import youtube_get_transcript as youtube_transcript_impl

        if languages is None:
            languages = ['hr', 'en']

        result = await youtube_transcript_impl(
            credentials=None,
            url=url,
            languages=languages,
            preserve_formatting=preserve_formatting
        )
        return result
    except Exception as e:
        logger.error(f"YouTube transcript extraction failed: {e}")
        return {"error": str(e), "url": url}


async def scrape_url(
    url: str,
    extract_type: str = "article",
    return_html: bool = False
) -> dict:
    """
    Scrape content from a URL.

    Optimized for Croatian news portals (index.hr, jutarnji.hr, 24sata.hr, vecernji.hr).
    Extracts article text, titles, and links using intelligent parsing.

    Best for: reading full articles, extracting detailed content, analyzing news.

    Args:
        url: URL to scrape
        extract_type: Type of extraction - 'article' (default), 'all_text', or 'links'
        return_html: Return raw HTML as well (default: False)

    Returns:
        Dictionary with extracted content, title, and metadata
    """
    try:
        from tools.api_implementations.web_scraper_api import scrape_url as scrape_url_impl

        result = await scrape_url_impl(
            credentials=None,
            url=url,
            extract_type=extract_type,
            return_html=return_html
        )
        return _cap_scraped(result)
    except Exception as e:
        logger.error(f"URL scraping failed for {url}: {e}")
        return {"error": str(e), "url": url}


# --- Keep scraped pages from eating the context -----------------------------
# Every scrape result stays in the ReAct history and is resent on every
# subsequent call. Measured 2026-09-03: one research turn sent 1.18M input
# tokens across eight calls, mostly full page text repeated. A price or a spec
# is in the first few thousand characters; the rest is navigation and footer.

_SCRAPE_TEXT_FIELDS = ("content", "text", "markdown")


def _scrape_char_limit() -> int:
    try:
        return max(1000, int(os.getenv("SCRAPE_MAX_CHARS", "12000")))
    except ValueError:
        return 12000


def _cap_scraped(result: dict, limit: int = None) -> dict:
    """Truncate page text in place, and say so, so the model does not assume
    it read the whole page."""
    if not isinstance(result, dict):
        return result
    limit = _scrape_char_limit() if limit is None else limit
    for field in _SCRAPE_TEXT_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and len(value) > limit:
            result[field] = value[:limit].rstrip()
            result["truncated"] = True
            result["original_length"] = len(value)
            result["truncation_note"] = (
                f"Prikazano prvih {limit} znakova od {len(value)}. Ako traženi "
                "podatak nije ovdje, suzi upit ili otvori konkretniju podstranicu."
            )
    for nested in result.get("results") or []:
        _cap_scraped(nested, limit)
    return result


async def scrape_url_advanced(
    url: str,
    only_main_content: bool = True
) -> dict:
    """
    Advanced web scraping with automatic fallback chain. Handles JavaScript-rendered
    pages, tables, price lists, PDF documents, and sites that block standard scrapers.

    Tries in order:
    1. Jina Reader (free, no API key) — fast, works for most pages
    2. Firecrawl (requires FIRECRAWL_API_KEY) — for complex/protected pages

    Use when: scrape_url fails or returns empty/403, page uses React/Vue/AJAX,
    need to extract tables or price lists, target is a PDF from a web URL.

    Args:
        url: URL to scrape
        only_main_content: Strip nav/header/footer for cleaner output (default: True)

    Returns:
        Dictionary with content, word_count, url, source, success flag
    """
    try:
        from tools.api_implementations.web_scraper_api import scrape_url_jina, scrape_url_firecrawl

        # 1. Try Jina Reader first (free, no API key)
        result = await scrape_url_jina(None, url)
        if result.get("success"):
            return _cap_scraped(result)

        logger.info(f"Jina Reader failed for {url}, trying Firecrawl: {result.get('error')}")

        # 2. Fall back to Firecrawl
        result = await scrape_url_firecrawl(None, url, only_main_content=only_main_content)
        return _cap_scraped(result)

    except Exception as e:
        logger.error(f"Advanced URL scraping failed for {url}: {e}")
        return {"error": str(e), "url": url, "success": False}


async def scrape_multiple_urls(
    urls: List[str],
    extract_type: str = "article",
    validate_first: bool = True,
    skip_invalid: bool = True
) -> dict:
    """
    Scrape multiple URLs in parallel with smart validation.

    **NEW FEATURE**: Pre-validates URLs before scraping to skip 404s/broken links,
    saving time and improving success rate.

    Efficient for processing search results or news aggregation.
    Handles failures gracefully - returns partial results if some URLs fail.

    Best for: batch processing, comparing multiple sources, news aggregation.

    Args:
        urls: List of URLs to scrape
        extract_type: Type of extraction - 'article' (default), 'all_text', or 'links'
        validate_first: Pre-validate URL accessibility before scraping (default: True)
        skip_invalid: Skip invalid URLs (404, timeout, etc.) instead of attempting scrape (default: True)

    Returns:
        Dictionary with:
        - total_urls: Total number of URLs provided
        - validated: Number of URLs skipped due to validation
        - successful: Number of successfully scraped URLs
        - failed: Number of failed URLs
        - results: Array of results (both successful and failed)
    """
    try:
        from tools.api_implementations.web_scraper_api import scrape_multiple_urls as scrape_multiple_impl

        result = await scrape_multiple_impl(
            credentials=None,
            urls=urls,
            extract_type=extract_type,
            validate_first=validate_first,
            skip_invalid=skip_invalid
        )
        logger.info(
            f"Scraped {result.get('total_urls', 0)} URLs: "
            f"{result.get('successful', 0)} successful, "
            f"{result.get('validated', 0)} skipped (invalid)"
        )
        # A batch of pages multiplies the problem, so each page gets a
        # tighter slice than a single deliberate scrape.
        return _cap_scraped(result, limit=max(1000, _scrape_char_limit() // 3))
    except Exception as e:
        logger.error(f"Multiple URL scraping failed: {e}")
        return {"error": str(e), "urls": urls}


def get_research_adk_tools(credentials=None) -> List:
    """
    Get all Research ADK tools as plain Python functions.

    ADK automatically wraps these functions as tools based on:
    - Function signature (type hints)
    - Docstring (description and parameter docs)

    Args:
        credentials: Not used - included for API compatibility. Tools use env credentials.

    Returns:
        List of research tool functions
    """
    tools = [
        google_search_grounding,
        google_search_simple,
        youtube_get_transcript,
        scrape_url,
        scrape_multiple_urls,
        scrape_url_advanced,
    ]

    logger.info(f"Research ADK tools loaded: {len(tools)} tools")
    return tools


def get_research_capabilities() -> dict:
    """
    Get description of Research agent capabilities.

    Returns:
        Dictionary with capability descriptions
    """
    return {
        "web_search": {
            "description": "Google Search with Vertex AI Grounding",
            "tools": ["google_search_grounding", "google_search_simple"],
            "capabilities": [
                "Real-time web search",
                "Automatic source citation",
                "AI-generated summaries",
                "Recent information access"
            ],
            "no_api_key_needed": True
        },
        "youtube_analysis": {
            "description": "YouTube Transcript Extraction",
            "tools": ["youtube_get_transcript"],
            "capabilities": [
                "Extract video transcripts",
                "Croatian and English support",
                "Timestamp preservation",
                "Free access (no API key)"
            ],
            "no_api_key_needed": True
        },
        "web_scraping": {
            "description": "Web Content Extraction",
            "tools": ["scrape_url", "scrape_multiple_urls"],
            "capabilities": [
                "Article extraction",
                "Croatian news portal optimization",
                "Parallel URL processing",
                "Link extraction"
            ],
            "optimized_for": [
                "index.hr",
                "jutarnji.hr",
                "24sata.hr",
                "vecernji.hr",
                "rtl.hr"
            ],
            "no_api_key_needed": True
        }
    }


if __name__ == "__main__":
    # Test tool loading
    tools = get_research_adk_tools()
    print(f"✅ Research ADK tools loaded: {len(tools)} tools")

    for tool in tools:
        print(f"   - {tool.__name__}")

    print("\n📋 Capabilities:")
    caps = get_research_capabilities()
    for cap_name, cap_info in caps.items():
        print(f"\n   {cap_name.upper()}:")
        print(f"   └─ {cap_info['description']}")
        print(f"   └─ Tools: {', '.join(cap_info['tools'])}")
        print(f"   └─ No API key needed: {cap_info['no_api_key_needed']}")
