"""Raw web search that does not depend on the Google console being cooperative.

The researcher's whole method rests on getting a list of pages it can open. When
that list is missing it falls back to an AI summary, has nothing to scrape, and
searches again — measured 2026-09-03, eighteen searches and one batch of scrapes
in a single run.

Custom Search was meant to be that list. It answers 403 "This project does not
have the access to Custom Search JSON API" on a project where the API is enabled
and the key belongs, with 29 requests and a 100% error rate on the metrics page —
a console-side state nothing in this repo can fix — and Google has since closed
that API to new customers altogether. So the search path has more than one
provider, and takes the first one that actually answers.

Every provider returns the same shape as google_custom_search, so callers do
not care which one answered:

    {"query", "answer", "sources": [{"url", "title", "snippet"}],
     "source_count", "search_method"}
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20


def provider_order(
    env_name: str,
    default: Iterable[str],
    known: Iterable[str],
    aliases: Optional[Dict[str, str]] = None,
) -> list:
    """Read a provider order such as ``SEARCH_PROVIDERS=jina,firecrawl,duckduckgo``.

    Unknown names are dropped with a warning rather than failing the call: a typo
    in .env should cost one provider, not the whole search path. An empty or
    entirely unusable value falls back to the default order.
    """
    known = set(known)
    aliases = aliases or {}
    raw = os.getenv(env_name, "")
    order = []
    for token in re.split(r"[,\s]+", raw.strip().lower()):
        if not token:
            continue
        name = aliases.get(token, token)
        if name not in known:
            logger.warning(
                "%s: unknown provider %r ignored (known: %s)",
                env_name, token, ", ".join(sorted(known)),
            )
            continue
        if name not in order:
            order.append(name)
    if not order:
        if raw.strip():
            logger.warning("%s=%r names no known provider; using the default order", env_name, raw)
        return list(default)
    return order


def jina_api_key() -> Optional[str]:
    return os.getenv("JINA_API_KEY") or None


def firecrawl_api_key() -> Optional[str]:
    return os.getenv("FIRECRAWL_API_KEY") or None


_FIRECRAWL_API = "https://api.firecrawl.dev"


async def firecrawl_post(path: str, payload: dict, api_key: str, timeout_seconds: float) -> tuple:
    """POST to the Firecrawl REST API and return ``(status, body)``.

    REST rather than the firecrawl-py SDK: its surface moved between 4.8 (this
    laptop) and 4.38 (the Pi) — the scraper had to try V1FirecrawlApp, then
    FirecrawlApp, then two spellings of one argument. The HTTP API is the
    stable contract. The body is the parsed JSON, or ``{"error": text}`` when
    the answer is not JSON (a gateway page, say).
    """
    import aiohttp

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(_FIRECRAWL_API + path, json=payload, headers=headers) as response:
            text = await response.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = {"error": text[:200]}
            if not isinstance(body, dict):
                body = {"data": body}
            return response.status, body


def firecrawl_error(status: int, body: dict) -> str:
    """Name the failures that matter, so a log line says why the chain moved on."""
    detail = str(body.get("error") or body.get("message") or "")[:200]
    if status == 402:
        # The free plan is 1,000 credits a month; this is what running out looks
        # like, and the next provider takes over without anyone noticing.
        return f"Firecrawl credits exhausted (HTTP 402): {detail}"
    if status == 429:
        return f"Firecrawl rate limit (HTTP 429): {detail}"
    if status in (401, 403):
        return f"Firecrawl key rejected (HTTP {status})"
    return f"Firecrawl HTTP {status}: {detail}"


def _empty(query: str, error: str) -> Dict[str, Any]:
    return {"query": query, "error": error, "sources": [], "source_count": 0}


def _packed(query: str, sources: list, method: str) -> Dict[str, Any]:
    answer_parts = [f"Search results for '{query}':\n"]
    for i, source in enumerate(sources[:5], 1):
        answer_parts.append(f"{i}. {source.get('title') or source['url']}")
        if source.get("snippet"):
            answer_parts.append(f"   {source['snippet']}")
        answer_parts.append(f"   URL: {source['url']}\n")
    return {
        "query": query,
        "answer": "\n".join(answer_parts),
        "sources": sources,
        "source_count": len(sources),
        "search_method": method,
    }


async def jina_search(query: str, num_results: int = 10, locale: Optional[str] = None) -> Dict[str, Any]:
    """Search via s.jina.ai. Needs JINA_API_KEY (the free tier is enough)."""
    key = jina_api_key()
    if not key:
        return _empty(query, "JINA_API_KEY not configured")

    try:
        import aiohttp

        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            # Titles, URLs and descriptions only: the page bodies would be
            # dozens of KB each and this result goes into the ReAct history.
            "X-Respond-With": "no-content",
        }
        if locale:
            headers["X-Locale"] = locale

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://s.jina.ai/", params={"q": query}, headers=headers
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    return _empty(query, f"Jina search HTTP {response.status}: {body}")
                data = await response.json()

        items = data.get("data") or []
        sources = []
        for item in items[:num_results]:
            url = item.get("url")
            if not url:
                continue
            sources.append({
                "url": url,
                "title": item.get("title"),
                "snippet": item.get("description") or item.get("snippet"),
            })
        if not sources:
            return _empty(query, "Jina search returned no results")
        return _packed(query, sources, "jina_search")

    except Exception as exc:
        logger.warning("Jina search failed: %s", exc)
        return _empty(query, str(exc))


async def firecrawl_search(query: str, num_results: int = 10, locale: Optional[str] = None) -> Dict[str, Any]:
    """Search via Firecrawl. Needs FIRECRAWL_API_KEY.

    Measured on the Pi 2026-09-13 with four Croatian queries: the same hits as
    Jina, in 0.9-1.4 s against Jina's 1.7-4.4 s. It is still second in the
    default order because every search spends credits (the free plan has 1,000 a
    month) that page reading needs more — Jina search costs nothing.
    """
    key = firecrawl_api_key()
    if not key:
        return _empty(query, "FIRECRAWL_API_KEY not configured")

    try:
        limit = max(1, min(int(num_results or 10), 20))
    except (TypeError, ValueError):
        limit = 10
    payload: Dict[str, Any] = {"query": query, "limit": limit}
    if locale == "hr":
        payload["location"] = "Croatia"

    try:
        status, body = await firecrawl_post("/v2/search", payload, key, _TIMEOUT_SECONDS)
        if status != 200:
            return _empty(query, firecrawl_error(status, body))
        if body.get("success") is False:
            return _empty(query, f"Firecrawl search failed: {body.get('error')}")

        data = body.get("data") or {}
        # v2 answers {"web": [...], "news": [...]}; v1 answered a bare list.
        items = data.get("web") if isinstance(data, dict) else data
        sources = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url or any(s["url"] == url for s in sources):
                continue
            sources.append({
                "url": url,
                "title": item.get("title"),
                "snippet": item.get("description") or item.get("snippet"),
            })
            if len(sources) >= limit:
                break
        if not sources:
            return _empty(query, "Firecrawl search returned no results")
        return _packed(query, sources, "firecrawl_search")

    except Exception as exc:
        logger.warning("Firecrawl search failed: %s", exc)
        # A timeout stringifies to "", which would read as success-with-nothing.
        return _empty(query, str(exc) or type(exc).__name__)


# DuckDuckGo's HTML endpoint wraps every result in a redirect:
#   //duckduckgo.com/l/?uddg=<percent-encoded target>&rut=...
_DDG_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_DDG_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment or "")).strip()


def _unwrap_ddg(href: str) -> Optional[str]:
    """Pull the real target out of DuckDuckGo's redirect wrapper."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    target = parse_qs(parsed.query).get("uddg", [None])[0]
    if target:
        return unquote(target)
    return href if parsed.scheme in ("http", "https") else None


def parse_duckduckgo_html(markup: str, num_results: int = 10) -> list:
    """Separated from the request so the brittle half can be tested offline."""
    snippets = [_clean(s) for s in _DDG_SNIPPET.findall(markup)]
    sources = []
    for index, (href, title) in enumerate(_DDG_RESULT.findall(markup)):
        url = _unwrap_ddg(href)
        if not url:
            continue
        if any(s["url"] == url for s in sources):
            continue
        sources.append({
            "url": url,
            "title": _clean(title),
            "snippet": snippets[index] if index < len(snippets) else None,
        })
        if len(sources) >= num_results:
            break
    return sources


async def duckduckgo_search(query: str, num_results: int = 10, locale: Optional[str] = None) -> Dict[str, Any]:
    """Search DuckDuckGo's HTML endpoint. No key, no signup.

    This parses a search page, so it is the last resort before giving up on raw
    results: a markup change breaks it, which is why the parser is isolated and
    a failure degrades to the grounding path rather than raising.
    """
    try:
        import aiohttp

        params = {"q": query}
        if locale:
            params["kl"] = f"{locale}-{locale}"

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; JarvisResearch/1.0)"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://html.duckduckgo.com/html/", params=params, headers=headers
            ) as response:
                if response.status != 200:
                    return _empty(query, f"DuckDuckGo HTTP {response.status}")
                markup = await response.text()

        sources = parse_duckduckgo_html(markup, num_results)
        if not sources:
            return _empty(query, "DuckDuckGo returned no parsable results")
        return _packed(query, sources, "duckduckgo_search")

    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return _empty(query, str(exc))
