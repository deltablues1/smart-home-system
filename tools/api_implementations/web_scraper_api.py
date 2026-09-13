"""
Web Scraper API Implementation

Real implementation for web scraping using BeautifulSoup and requests
Optimized for Croatian news portals (index.hr, jutarnji.hr, 24sata.hr, etc.)
"""

import logging
import re
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
import asyncio

logger = logging.getLogger(__name__)


# Croatian news portals configuration
CROATIAN_NEWS_PORTALS = {
    'index.hr': {
        'article_selector': 'article, div.article-content, div.content',
        'title_selector': 'h1, h1.title',
        'text_selector': 'p, div.text'
    },
    'jutarnji.hr': {
        'article_selector': 'article, div.article-body',
        'title_selector': 'h1',
        'text_selector': 'p'
    },
    '24sata.hr': {
        'article_selector': 'article, div.article__body',
        'title_selector': 'h1',
        'text_selector': 'p'
    },
    'vecernji.hr': {
        'article_selector': 'article',
        'title_selector': 'h1',
        'text_selector': 'p'
    },
    'rtl.hr': {
        'article_selector': 'article, div.article-content',
        'title_selector': 'h1',
        'text_selector': 'p'
    }
}


def clean_text(text: str) -> str:
    """
    Clean scraped text by removing extra whitespace and normalizing

    Args:
        text: Raw text

    Returns:
        Cleaned text
    """
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove leading/trailing whitespace
    text = text.strip()
    return text


async def validate_url(url: str, timeout: int = 5) -> Dict[str, Any]:
    """
    Validate URL accessibility using HEAD request before scraping

    Args:
        url: URL to validate
        timeout: Request timeout in seconds

    Returns:
        Dictionary with validation result:
        - valid: Boolean indicating if URL is accessible
        - status_code: HTTP status code
        - error: Error message if validation failed
    """
    try:
        import requests

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        # Use HEAD request for fast validation
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        )

        if response.status_code in (200, 301, 302):
            return {"valid": True, "status_code": response.status_code}
        elif response.status_code in (403, 405, 406, 429):
            # Many webshops block HEAD but allow GET — try GET with stream to avoid downloading full page
            try:
                get_resp = await loop.run_in_executor(
                    None,
                    lambda: requests.get(url, headers=headers, timeout=timeout, stream=True)
                )
                get_resp.close()
                valid = get_resp.status_code in (200, 301, 302)
                return {"valid": valid, "status_code": get_resp.status_code}
            except Exception:
                # If GET also fails, still try scraping — validator is best-effort
                return {"valid": True, "status_code": response.status_code, "note": "HEAD blocked, GET failed, will attempt scrape"}
        elif response.status_code in (404, 410, 451):
            # Definitively gone
            return {"valid": False, "status_code": response.status_code, "error": f"HTTP {response.status_code}"}
        else:
            # Unknown status — optimistically try scraping rather than skipping
            return {"valid": True, "status_code": response.status_code, "note": "Unknown status, will attempt scrape"}

    except requests.exceptions.Timeout:
        return {"valid": False, "status_code": None, "error": "Timeout"}
    except requests.exceptions.RequestException as e:
        return {"valid": False, "status_code": None, "error": str(e)}
    except Exception as e:
        return {"valid": False, "status_code": None, "error": str(e)}


async def scrape_url(
    credentials,
    url: str,
    extract_type: str = "article",
    return_html: bool = False,
    validate_first: bool = True
) -> Dict[str, Any]:
    """
    Scrape content from a URL using BeautifulSoup

    Optimized for:
    - Croatian news portals (index.hr, jutarnji.hr, 24sata.hr, etc.)
    - General web pages
    - Article extraction

    Args:
        credentials: Not used (kept for consistency)
        url: URL to scrape
        extract_type: Type of extraction:
            - "article": Extract article content (default)
            - "all_text": Extract all text from page
            - "links": Extract all links
        return_html: Return raw HTML as well (default: False)
        validate_first: Validate URL accessibility before scraping (default: True)

    Returns:
        Dictionary with:
        - url: Original URL
        - title: Page/article title
        - text: Extracted text content
        - word_count: Number of words
        - links: List of links (if extract_type="links")
        - html: Raw HTML (if return_html=True)
        - portal: Detected portal name (if Croatian news portal)

    Example:
        result = await scrape_url(
            credentials=None,
            url="https://www.index.hr/vijesti/..."
        )
    """
    try:
        # Import required libraries
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:
            return {
                "error": "Required libraries not installed. Run: pip install requests beautifulsoup4",
                "url": url,
                "text": None
            }

        # Validate URL first if requested
        if validate_first:
            validation = await validate_url(url)
            if not validation.get("valid"):
                error_msg = validation.get("error", "URL not accessible")
                status_code = validation.get("status_code")

                # Use WARNING for common client errors (404, 403), ERROR for others
                if status_code in [404, 403, 410]:
                    logger.warning(f"URL not accessible [{status_code}]: {url}")
                else:
                    logger.error(f"URL validation failed: {url} - {error_msg}")

                return {
                    "error": error_msg,
                    "url": url,
                    "status_code": status_code,
                    "text": None
                }

        logger.info(f"Scraping URL: {url}")

        # Set headers to mimic a browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'hr-HR,hr;q=0.9,en-US;q=0.8,en;q=0.7',
        }

        # Fetch the page (run in executor to avoid blocking)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(url, headers=headers, timeout=10)
        )
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')

        # Detect if it's a Croatian news portal
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.replace('www.', '')
        portal_config = CROATIAN_NEWS_PORTALS.get(domain)

        # Extract title
        title = None
        if portal_config:
            # Use portal-specific selector
            title_elem = soup.select_one(portal_config['title_selector'])
            if title_elem:
                title = clean_text(title_elem.get_text())

        if not title:
            # Fallback to standard title
            title_elem = soup.find('title') or soup.find('h1')
            if title_elem:
                title = clean_text(title_elem.get_text())

        # Extract content based on type
        text_content = ""
        links = []

        if extract_type == "article" and portal_config:
            # Use portal-specific selectors for article content
            article_elem = soup.select_one(portal_config['article_selector'])
            if article_elem:
                # Extract paragraphs from article
                paragraphs = article_elem.select(portal_config['text_selector'])
                text_content = "\n\n".join([clean_text(p.get_text()) for p in paragraphs if p.get_text().strip()])
            else:
                # Fallback to all paragraphs
                paragraphs = soup.find_all('p')
                text_content = "\n\n".join([clean_text(p.get_text()) for p in paragraphs if p.get_text().strip()])

        elif extract_type == "article":
            # Generic article extraction
            # Remove script, style, nav, footer
            for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                element.decompose()

            # Try to find article tag
            article = soup.find('article') or soup.find('main') or soup

            # Extract paragraphs
            paragraphs = article.find_all('p')
            text_content = "\n\n".join([clean_text(p.get_text()) for p in paragraphs if p.get_text().strip()])

        elif extract_type == "all_text":
            # Extract all text
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'footer', 'header']):
                element.decompose()

            text_content = clean_text(soup.get_text())

        elif extract_type == "links":
            # Extract all links
            all_links = soup.find_all('a', href=True)
            links = [
                {
                    'url': link['href'],
                    'text': clean_text(link.get_text()) if link.get_text() else None
                }
                for link in all_links
            ]
            text_content = f"Found {len(links)} links"

        # Count words
        word_count = len(text_content.split())

        result = {
            "url": url,
            "title": title,
            "text": text_content,
            "word_count": word_count,
            "portal": domain if portal_config else None,
            "extract_type": extract_type
        }

        if extract_type == "links":
            result["links"] = links
            result["link_count"] = len(links)

        if return_html:
            result["html"] = str(soup)

        logger.info(f"Scraping completed: {word_count} words extracted from {url}")

        # If too few words extracted, webshop is likely JS-rendered — auto-fallback to Jina Reader
        if word_count < 50:
            logger.info(f"Too few words ({word_count}) from {url} — trying Jina Reader fallback")
            jina_result = await scrape_url_jina(None, url)
            if jina_result.get("success") and jina_result.get("word_count", 0) > word_count:
                logger.info(f"Jina Reader fallback successful: {jina_result['word_count']} words from {url}")
                return jina_result

        return result

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout while scraping {url}")
        return {
            "error": "Request timeout (10s)",
            "url": url,
            "text": None
        }

    except requests.exceptions.HTTPError as e:
        # HTTP errors (404, 403, etc.) - use WARNING for client errors
        status_code = e.response.status_code if hasattr(e, 'response') else None
        if status_code and 400 <= status_code < 500:
            logger.warning(f"HTTP {status_code} error for {url}: {e}")
        else:
            logger.error(f"HTTP error for {url}: {e}")
        return {
            "error": f"HTTP error: {str(e)}",
            "url": url,
            "status_code": status_code,
            "text": None
        }

    except requests.exceptions.RequestException as e:
        logger.warning(f"Request failed for {url}: {e}")
        return {
            "error": f"Request failed: {str(e)}",
            "url": url,
            "text": None
        }

    except Exception as e:
        logger.error(f"Web scraping failed for {url}: {e}")
        return {
            "error": str(e),
            "url": url,
            "text": None
        }


async def scrape_multiple_urls(
    credentials,
    urls: List[str],
    extract_type: str = "article",
    validate_first: bool = True,
    skip_invalid: bool = True
) -> Dict[str, Any]:
    """
    Scrape multiple URLs in parallel with optional validation

    Args:
        credentials: Not used
        urls: List of URLs to scrape
        extract_type: Type of extraction (article, all_text, links)
        validate_first: Validate URLs before scraping (default: True)
        skip_invalid: Skip invalid URLs instead of attempting scrape (default: True)

    Returns:
        Dictionary with results for each URL
    """
    try:
        logger.info(f"Scraping {len(urls)} URLs in parallel")

        # Pre-validate all URLs if requested
        valid_urls = []
        invalid_urls = []

        if validate_first and skip_invalid:
            logger.info(f"Pre-validating {len(urls)} URLs...")
            validation_tasks = [validate_url(url) for url in urls]
            validations = await asyncio.gather(*validation_tasks, return_exceptions=True)

            for url, validation in zip(urls, validations):
                if isinstance(validation, Exception) or not validation.get("valid"):
                    invalid_urls.append({
                        "url": url,
                        "error": validation.get("error", str(validation)) if isinstance(validation, dict) else str(validation),
                        "success": False
                    })
                else:
                    valid_urls.append(url)

            logger.info(f"Validation complete: {len(valid_urls)} valid, {len(invalid_urls)} invalid")

            if len(valid_urls) == 0:
                logger.warning("No valid URLs to scrape")
                return {
                    "total_urls": len(urls),
                    "validated": len(invalid_urls),
                    "successful": 0,
                    "failed": len(invalid_urls),
                    "results": invalid_urls
                }
        else:
            valid_urls = urls

        # Scrape valid URLs in parallel
        tasks = [
            scrape_url(credentials, url, extract_type, validate_first=False)  # Skip validation since we already did it
            for url in valid_urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Format results
        output = {
            "total_urls": len(urls),
            "validated": len(invalid_urls) if validate_first and skip_invalid else 0,
            "successful": 0,
            "failed": len(invalid_urls),
            "results": invalid_urls.copy()  # Start with invalid URLs
        }

        for url, result in zip(valid_urls, results):
            if isinstance(result, Exception):
                output["results"].append({
                    "url": url,
                    "error": str(result),
                    "success": False
                })
                output["failed"] += 1
            elif "error" in result:
                output["results"].append({**result, "success": False})
                output["failed"] += 1
            else:
                output["results"].append({**result, "success": True})
                output["successful"] += 1

        logger.info(f"Scraping complete: {output['successful']}/{output['total_urls']} successful, {output['validated']} skipped (invalid)")
        return output

    except Exception as e:
        logger.error(f"Bulk scraping failed: {e}")
        return {
            "error": str(e),
            "total_urls": len(urls),
            "successful": 0,
            "failed": len(urls)
        }


async def scrape_url_jina(
    credentials,
    url: str,
) -> dict:
    """
    Scrape a URL using Jina Reader (r.jina.ai). Works with no API key at all;
    JINA_API_KEY, when set, raises the rate limit — which matters because a
    research run opens several pages in quick succession and an anonymous
    caller gets throttled first.

    Returns clean Markdown. Handles most static and moderately dynamic pages.

    Args:
        credentials: Not used, included for API compatibility
        url: URL to scrape

    Returns:
        Dictionary with content, word_count, url, source, success flag
    """
    import os as _os
    import requests as _requests
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
    }
    _jina_key = _os.getenv("JINA_API_KEY")
    if _jina_key:
        headers["Authorization"] = f"Bearer {_jina_key}"
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: _requests.get(jina_url, headers=headers, timeout=30)
        )
        if resp.status_code != 200:
            return {"error": f"Jina Reader returned HTTP {resp.status_code}", "url": url, "success": False}
        content = resp.text.strip()
        if not content or len(content) < 100:
            return {"error": "Jina Reader returned empty or too-short content", "url": url, "success": False}
        logger.info(f"Jina Reader scraped {url}: {len(content.split())} words")
        return {
            "url": url,
            "content": content,
            "word_count": len(content.split()),
            "source": "jina_reader",
            "success": True
        }
    except Exception as e:
        logger.error(f"Jina Reader scrape failed for {url}: {e}")
        return {"error": str(e), "url": url, "success": False}


async def scrape_url_firecrawl(
    credentials,
    url: str,
    formats: list = None,
    only_main_content: bool = True
) -> dict:
    """
    Scrape a URL using Firecrawl — handles JS-rendered pages, tables, price lists,
    PDFs, and complex layouts that BeautifulSoup cannot parse.

    Use when: standard scrape_url fails, page is a SPA/AJAX portal, need to extract
    tables or price lists, or target is a PDF linked from a web page.

    Requires FIRECRAWL_API_KEY environment variable.

    Args:
        credentials: Not used, included for API compatibility
        url: URL to scrape
        formats: Output formats list (default: ['markdown'])
        only_main_content: Strip nav/header/footer (default: True)

    Returns:
        Dictionary with content, word_count, url, source, success flag
    """
    import os
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        logger.warning("FIRECRAWL_API_KEY not set — cannot use Firecrawl scraper")
        return {"error": "FIRECRAWL_API_KEY not set in environment", "url": url, "success": False}
    try:
        # firecrawl-py 4.x moved the scraping API onto the v1 compatibility class;
        # FirecrawlApp there only exposes paper/GitHub search and parse(). Older
        # 0.x/1.x releases keep scrape_url on FirecrawlApp itself.
        try:
            from firecrawl import V1FirecrawlApp as _FirecrawlClient
        except ImportError:
            from firecrawl import FirecrawlApp as _FirecrawlClient

        app = _FirecrawlClient(api_key=api_key)

        def _scrape():
            # 4.x takes only_main_content, older releases took onlyMainContent.
            try:
                return app.scrape_url(
                    url,
                    formats=formats or ["markdown"],
                    only_main_content=only_main_content,
                )
            except TypeError:
                return app.scrape_url(
                    url,
                    formats=formats or ["markdown"],
                    onlyMainContent=only_main_content,
                )

        # scrape_url is blocking. Awaiting it directly would stall the event loop
        # that also serves Telegram, voice and the Home Assistant agent, for as
        # long as the remote page takes.
        result = await asyncio.to_thread(_scrape)

        content = (
            getattr(result, "markdown", None)
            or (result.get("markdown") if isinstance(result, dict) else None)
            or str(result)
        )
        logger.info(f"Firecrawl scraped {url}: {len(content.split())} words")
        return {
            "url": url,
            "content": content,
            "word_count": len(content.split()),
            "source": "firecrawl",
            "success": True
        }
    except Exception as e:
        logger.error(f"Firecrawl scrape failed for {url}: {e}")
        return {"error": str(e), "url": url, "success": False}


def register_web_scraper_tools(tool_registry) -> None:
    """
    Register web scraper tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # Register single URL scraper
    tool_registry.register_tool(
        name="scrape_url",
        function=scrape_url,
        description="Scrape content from a URL. Optimized for Croatian news portals (index.hr, jutarnji.hr, 24sata.hr, vecernji.hr). Extracts article text, titles, and links.",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to scrape"
                },
                "extract_type": {
                    "type": "string",
                    "description": "Type of extraction: 'article' (default), 'all_text', or 'links'",
                    "enum": ["article", "all_text", "links"],
                    "default": "article"
                },
                "return_html": {
                    "type": "boolean",
                    "description": "Return raw HTML as well (default: false)",
                    "default": False
                }
            },
            "required": ["url"]
        },
        requires_auth=False,
        auth_type="none"
    )

    # Register multiple URL scraper
    tool_registry.register_tool(
        name="scrape_multiple_urls",
        function=scrape_multiple_urls,
        description="Scrape multiple URLs in parallel. Efficient for processing search results or news aggregation.",
        parameters={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs to scrape"
                },
                "extract_type": {
                    "type": "string",
                    "description": "Type of extraction: 'article' (default), 'all_text', or 'links'",
                    "enum": ["article", "all_text", "links"],
                    "default": "article"
                }
            },
            "required": ["urls"]
        },
        requires_auth=False,
        auth_type="none"
    )

    logger.info("Web scraper tools registered successfully")
