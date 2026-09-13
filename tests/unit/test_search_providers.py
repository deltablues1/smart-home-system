"""
The researcher needs a list of pages, whoever provides it.

Custom Search answers 403 "This project does not have the access to Custom
Search JSON API" on a project where the API is enabled and the key belongs —
29 requests, 100% errors on the metrics page. That is console-side state no
code here can fix, and without a page list the researcher falls back to an AI
summary, has nothing to open, and searches again: eighteen searches in one run
on 2026-09-03.

So the path has several providers and takes the first that is configured.

Run with:
    pytest tests/unit/test_search_providers.py -v
"""

import pytest

from tools.api_implementations import google_search_api as api
from tools.api_implementations import search_providers as sp

DDG_HTML = """
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fklimatizacija.hr%2Fdizalica%2Dtopline%2F&amp;rut=abc">
     Dizalice topline &ndash; <b>cijena</b>
  </a>
  <a class="result__snippet">Cijene od <b>4.199</b> EUR s PDV-om.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnjuskalo.hr%2Fdizalice">Njuškalo</a>
  <a class="result__snippet">Oglasi</a>
</div>
"""


class TestDuckDuckGoParsing:
    """Isolated from the request, because this is the brittle half."""

    def test_urls_are_unwrapped_from_the_redirect(self):
        sources = sp.parse_duckduckgo_html(DDG_HTML)
        assert sources[0]["url"] == "https://klimatizacija.hr/dizalica-topline/"
        assert sources[1]["url"] == "https://njuskalo.hr/dizalice"

    def test_titles_lose_their_markup_and_entities(self):
        assert sp.parse_duckduckgo_html(DDG_HTML)[0]["title"] == "Dizalice topline – cijena"

    def test_snippets_come_through(self):
        assert "4.199" in sp.parse_duckduckgo_html(DDG_HTML)[0]["snippet"]

    def test_the_result_count_is_honoured(self):
        assert len(sp.parse_duckduckgo_html(DDG_HTML, num_results=1)) == 1

    def test_markup_it_cannot_read_yields_nothing_rather_than_raising(self):
        assert sp.parse_duckduckgo_html("<html>redesigned</html>") == []

    def test_duplicate_urls_are_collapsed(self):
        doubled = DDG_HTML + DDG_HTML
        urls = [s["url"] for s in sp.parse_duckduckgo_html(doubled)]
        assert len(urls) == len(set(urls))


class TestJinaNeedsAKey:
    @pytest.mark.asyncio
    async def test_without_a_key_it_reports_and_steps_aside(self, monkeypatch):
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        result = await sp.jina_search("cijene")
        assert result["error"] == "JINA_API_KEY not configured"
        assert result["sources"] == []


class TestProviderChain:
    def _chain_names(self):
        return [name for name, _ in api._raw_search_providers()]

    def test_order_is_custom_then_jina_then_ddg(self):
        assert self._chain_names() == [
            "custom_search_api", "jina_search", "duckduckgo_search"
        ]

    @pytest.mark.asyncio
    async def test_the_first_working_provider_wins(self, monkeypatch):
        async def ok(query, num_results, locale):
            return {"sources": [{"url": "https://a.hr", "title": "A", "snippet": "s"}],
                    "source_count": 1, "search_method": "custom_search_api"}

        async def never(query, num_results, locale):
            raise AssertionError("must not be reached")

        monkeypatch.setattr(api, "_raw_search_providers",
                            lambda: [("custom_search_api", ok), ("jina_search", never)])

        result = await api.google_search_simple(None, "cijene")
        assert result["search_method"] == "custom_search_api"
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_a_failing_provider_is_skipped(self, monkeypatch):
        async def dead(query, num_results, locale):
            return {"error": "403 no access", "sources": [], "source_count": 0}

        async def alive(query, num_results, locale):
            return {"sources": [{"url": "https://b.hr", "title": "B"}],
                    "source_count": 1, "search_method": "duckduckgo_search"}

        monkeypatch.setattr(api, "_raw_search_providers",
                            lambda: [("custom_search_api", dead), ("duckduckgo_search", alive)])

        result = await api.google_search_simple(None, "cijene")
        assert result["search_method"] == "duckduckgo_search"

    @pytest.mark.asyncio
    async def test_all_providers_down_falls_back_and_says_so(self, monkeypatch):
        async def dead(query, num_results, locale):
            return {"error": "down", "sources": [], "source_count": 0}

        async def grounding(credentials=None, query="", max_results=5, **_kw):
            return {"sources": [{"url": "https://c.hr"}], "source_count": 1}

        monkeypatch.setattr(api, "_raw_search_providers",
                            lambda: [("custom_search_api", dead), ("duckduckgo_search", dead)])
        monkeypatch.setattr(api, "google_search_grounding", grounding)

        result = await api.google_search_simple(None, "cijene")
        assert result["search_method"] == "vertex_ai_grounding_fallback"
        assert "warning" in result
        assert len(result["providers_tried"]) == 2


class TestJinaKeyIsUsedForScrapingToo:
    """A research run opens several pages in a row; the anonymous caller is the
    one that gets throttled first."""

    def _headers_for(self, monkeypatch, key):
        import requests

        captured = {}

        class _Resp:
            status_code = 200
            text = "x" * 200

        def _fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers or {}
            return _Resp()

        monkeypatch.setattr(requests, "get", _fake_get)
        if key is None:
            monkeypatch.delenv("JINA_API_KEY", raising=False)
        else:
            monkeypatch.setenv("JINA_API_KEY", key)

        import asyncio

        from tools.api_implementations.web_scraper_api import scrape_url_jina

        asyncio.run(scrape_url_jina(None, "https://example.hr"))
        return captured["headers"]

    def test_the_key_is_sent_when_present(self, monkeypatch):
        headers = self._headers_for(monkeypatch, "jina_testkey")
        assert headers["Authorization"] == "Bearer jina_testkey"

    def test_it_still_works_anonymously(self, monkeypatch):
        headers = self._headers_for(monkeypatch, None)
        assert "Authorization" not in headers
