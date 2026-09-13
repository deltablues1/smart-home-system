"""
A page is read by whoever reads it best, and the next one takes over when that fails.

Measured 2026-09-13 on three shop pages: the direct BeautifulSoup pass returned
138, 54 and 1,095 words and not one price — the Jina fallback only fired below
50 words — while Firecrawl and Jina Reader both found the prices. So a reader
leads, Firecrawl first. Firecrawl runs on a monthly credit budget (1,000 on the
free plan), so running out has to hand over to Jina without anyone noticing.

Run with:
    pytest tests/unit/test_scrape_providers.py -v
"""

from types import SimpleNamespace

import pytest

from tools.api_implementations import search_providers as sp
from tools.api_implementations import web_scraper_api as ws

SHOP = "https://shop.hr/dizalica-topline"
PORTAL = "https://www.index.hr/vijesti/clanak/nesto/123.aspx"


def _read(source, words=200):
    return {"url": SHOP, "content": "riječ " * words, "word_count": words,
            "source": source, "success": True}


def _direct(words=200):
    return {"url": SHOP, "text": "riječ " * words, "word_count": words, "source": "direct"}


def _failed(error):
    return {"url": SHOP, "error": error, "success": False}


@pytest.fixture
def chain(monkeypatch):
    """Stub the three providers and record who was asked, in order."""
    state = SimpleNamespace(
        calls=[],
        firecrawl=_read("firecrawl"),
        jina=_read("jina_reader"),
        direct=_direct(),
    )

    async def firecrawl(credentials, url, formats=None, only_main_content=True):
        state.calls.append("firecrawl")
        return state.firecrawl

    async def jina(credentials, url):
        state.calls.append("jina")
        return state.jina

    async def direct(url, extract_type="article", return_html=False, validate_first=True):
        state.calls.append("direct")
        return state.direct

    monkeypatch.setattr(ws, "scrape_url_firecrawl", firecrawl)
    monkeypatch.setattr(ws, "scrape_url_jina", jina)
    monkeypatch.setattr(ws, "_scrape_direct", direct)
    monkeypatch.delenv("SCRAPE_PROVIDERS", raising=False)
    return state


class TestProviderOrderFromEnv:
    KNOWN = ("a", "b", "c")

    def test_unset_means_the_default(self, monkeypatch):
        monkeypatch.delenv("X_PROVIDERS", raising=False)
        assert sp.provider_order("X_PROVIDERS", ("a", "b"), self.KNOWN) == ["a", "b"]

    def test_the_env_reorders_and_accepts_aliases(self, monkeypatch):
        monkeypatch.setenv("X_PROVIDERS", " C , bee")
        assert sp.provider_order("X_PROVIDERS", ("a",), self.KNOWN, {"bee": "b"}) == ["c", "b"]

    def test_a_typo_costs_one_provider_not_the_chain(self, monkeypatch):
        monkeypatch.setenv("X_PROVIDERS", "a,bing")
        assert sp.provider_order("X_PROVIDERS", ("b",), self.KNOWN) == ["a"]

    def test_nothing_usable_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("X_PROVIDERS", "bing, yahoo")
        assert sp.provider_order("X_PROVIDERS", ("a", "b"), self.KNOWN) == ["a", "b"]

    def test_duplicates_collapse(self, monkeypatch):
        monkeypatch.setenv("X_PROVIDERS", "a,a,b")
        assert sp.provider_order("X_PROVIDERS", ("c",), self.KNOWN) == ["a", "b"]

    def test_scrape_default_is_firecrawl_jina_direct(self, monkeypatch):
        monkeypatch.delenv("SCRAPE_PROVIDERS", raising=False)
        assert ws.scrape_provider_order() == ["firecrawl", "jina", "direct"]

    def test_scrape_aliases(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_PROVIDERS", "jina_reader,bs4")
        assert ws.scrape_provider_order() == ["jina", "direct"]


def _fake_post(monkeypatch, status, body):
    sent = {}

    async def post(path, payload, api_key, timeout_seconds):
        sent.update(path=path, payload=payload, api_key=api_key)
        return status, body

    monkeypatch.setattr(sp, "firecrawl_post", post)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    return sent


class TestFirecrawlSearch:
    @pytest.mark.asyncio
    async def test_without_a_key_it_steps_aside(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        result = await sp.firecrawl_search("cijene")
        assert result["error"] == "FIRECRAWL_API_KEY not configured"
        assert result["sources"] == []

    @pytest.mark.asyncio
    async def test_v2_results_become_the_common_shape(self, monkeypatch):
        _fake_post(monkeypatch, 200, {"success": True, "data": {"web": [
            {"url": "https://a.hr", "title": "A", "description": "4.199 EUR"},
            {"url": "https://a.hr", "title": "A again"},
            {"url": "https://b.hr", "title": "B"},
        ]}})
        result = await sp.firecrawl_search("cijene dizalica")
        assert result["search_method"] == "firecrawl_search"
        assert result["sources"][0] == {"url": "https://a.hr", "title": "A", "snippet": "4.199 EUR"}
        assert [s["url"] for s in result["sources"]] == ["https://a.hr", "https://b.hr"]

    @pytest.mark.asyncio
    async def test_the_v1_list_shape_still_reads(self, monkeypatch):
        _fake_post(monkeypatch, 200, {"success": True, "data": [{"url": "https://a.hr", "title": "A"}]})
        result = await sp.firecrawl_search("cijene")
        assert result["source_count"] == 1

    @pytest.mark.asyncio
    async def test_croatian_queries_ask_for_croatia(self, monkeypatch):
        sent = _fake_post(monkeypatch, 200, {"data": {"web": [{"url": "https://a.hr"}]}})
        await sp.firecrawl_search("cijene", locale="hr")
        assert sent["payload"]["location"] == "Croatia"

        await sp.firecrawl_search("heat pump prices")
        assert "location" not in sent["payload"]

    @pytest.mark.asyncio
    async def test_the_limit_is_bounded(self, monkeypatch):
        sent = _fake_post(monkeypatch, 200, {"data": {"web": [{"url": "https://a.hr"}]}})
        await sp.firecrawl_search("cijene", num_results=500)
        assert sent["payload"]["limit"] == 20

    @pytest.mark.asyncio
    async def test_running_out_of_credits_is_named(self, monkeypatch):
        _fake_post(monkeypatch, 402, {"error": "Insufficient credits"})
        result = await sp.firecrawl_search("cijene")
        assert "credits exhausted" in result["error"]
        assert result["sources"] == []

    @pytest.mark.asyncio
    async def test_a_timeout_is_still_an_error(self, monkeypatch):
        import asyncio

        async def post(*_args, **_kwargs):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(sp, "firecrawl_post", post)
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
        result = await sp.firecrawl_search("cijene")
        assert result["error"]


class TestFirecrawlScrape:
    @pytest.mark.asyncio
    async def test_without_a_key_it_steps_aside(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        result = await ws.scrape_url_firecrawl(None, SHOP)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_a_page_comes_back_with_its_title(self, monkeypatch):
        _fake_post(monkeypatch, 200, {"success": True, "data": {
            "markdown": "# Dizalica\n\nCijena: 4.199,00 EUR s PDV-om",
            "metadata": {"title": "Dizalica topline 8 kW", "statusCode": 200},
        }})
        result = await ws.scrape_url_firecrawl(None, SHOP)
        assert result["success"] is True
        assert result["source"] == "firecrawl"
        assert result["title"] == "Dizalica topline 8 kW"
        assert "4.199,00 EUR" in result["content"]

    @pytest.mark.asyncio
    async def test_it_asks_for_the_main_content_and_a_fresh_copy(self, monkeypatch):
        sent = _fake_post(monkeypatch, 200, {"data": {"markdown": "x " * 60, "metadata": {}}})
        await ws.scrape_url_firecrawl(None, SHOP)
        assert sent["path"] == "/v2/scrape"
        assert sent["payload"]["onlyMainContent"] is True
        # The API's own default is a copy up to two days old; the researcher
        # writes today's date next to every price.
        assert sent["payload"]["maxAge"] <= 3_600_000

    @pytest.mark.asyncio
    async def test_out_of_credits_is_a_failure_the_chain_can_see(self, monkeypatch):
        _fake_post(monkeypatch, 402, {"error": "Insufficient credits"})
        result = await ws.scrape_url_firecrawl(None, SHOP)
        assert result["success"] is False
        assert "credits exhausted" in result["error"]

    @pytest.mark.asyncio
    async def test_the_sites_error_page_is_not_the_page(self, monkeypatch):
        _fake_post(monkeypatch, 200, {"data": {"markdown": "Not found", "metadata": {"statusCode": 404}}})
        result = await ws.scrape_url_firecrawl(None, SHOP)
        assert result["success"] is False
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_empty_content_is_a_failure(self, monkeypatch):
        _fake_post(monkeypatch, 200, {"data": {"markdown": "  ", "metadata": {"statusCode": 200}}})
        assert (await ws.scrape_url_firecrawl(None, SHOP))["success"] is False


class TestScrapeOrder:
    @pytest.mark.asyncio
    async def test_a_shop_page_is_read_by_firecrawl_alone(self, chain):
        result = await ws.scrape_url(None, SHOP)
        assert chain.calls == ["firecrawl"]
        assert result["source"] == "firecrawl"

    @pytest.mark.asyncio
    async def test_out_of_credits_hands_over_to_jina(self, chain):
        chain.firecrawl = _failed("Firecrawl credits exhausted (HTTP 402)")
        result = await ws.scrape_url(None, SHOP)
        assert chain.calls == ["firecrawl", "jina"]
        assert result["source"] == "jina_reader"

    @pytest.mark.asyncio
    async def test_both_readers_down_leaves_the_direct_fetch(self, chain):
        chain.firecrawl = _failed("down")
        chain.jina = _failed("HTTP 451")
        result = await ws.scrape_url(None, SHOP)
        assert chain.calls == ["firecrawl", "jina", "direct"]
        assert result["source"] == "direct"

    @pytest.mark.asyncio
    async def test_a_thin_page_gives_the_next_provider_a_turn(self, chain):
        chain.firecrawl = _read("firecrawl", words=12)
        result = await ws.scrape_url(None, SHOP)
        assert chain.calls == ["firecrawl", "jina"]
        assert result["source"] == "jina_reader"

    @pytest.mark.asyncio
    async def test_when_every_page_is_thin_the_fullest_wins(self, chain):
        chain.firecrawl = _read("firecrawl", words=12)
        chain.jina = _read("jina_reader", words=30)
        chain.direct = _direct(words=5)
        result = await ws.scrape_url(None, SHOP)
        assert result["source"] == "jina_reader"

    @pytest.mark.asyncio
    async def test_a_news_portal_is_parsed_directly_first(self, chain):
        result = await ws.scrape_url(None, PORTAL)
        assert chain.calls == ["direct"]
        assert result["source"] == "direct"

    @pytest.mark.asyncio
    async def test_a_thin_portal_page_is_rescued_by_a_reader(self, chain):
        chain.direct = _direct(words=20)
        await ws.scrape_url(None, PORTAL)
        assert chain.calls == ["direct", "firecrawl"]

    @pytest.mark.asyncio
    async def test_links_never_go_to_a_reader(self, chain):
        # "Found 3 links" is always under 50 words; the old fallback swapped the
        # link list for Jina's page text.
        chain.direct = {"url": SHOP, "text": "Found 3 links", "word_count": 3,
                        "links": [{"url": "https://a.hr"}], "source": "direct"}
        result = await ws.scrape_url(None, SHOP, extract_type="links")
        assert chain.calls == ["direct"]
        assert result["links"]

    @pytest.mark.asyncio
    async def test_raw_html_is_direct_only(self, chain):
        await ws.scrape_url(None, SHOP, return_html=True)
        assert chain.calls == ["direct"]

    @pytest.mark.asyncio
    async def test_the_env_can_leave_firecrawl_out(self, chain, monkeypatch):
        monkeypatch.setenv("SCRAPE_PROVIDERS", "jina,direct")
        await ws.scrape_url(None, SHOP)
        assert chain.calls == ["jina"]

    @pytest.mark.asyncio
    async def test_everything_down_is_one_error_naming_each_provider(self, chain):
        chain.firecrawl = _failed("credits")
        chain.jina = _failed("HTTP 500")
        chain.direct = {"url": SHOP, "error": "HTTP error: 403", "status_code": 403, "text": None}
        result = await ws.scrape_url(None, SHOP)
        assert result["text"] is None
        assert [p.split(":")[0] for p in result["providers_tried"]] == ["firecrawl", "jina", "direct"]
        assert result["status_code"] == 403

    @pytest.mark.asyncio
    async def test_a_thin_page_after_a_404_is_not_a_page(self, chain):
        # Measured: a shop's 404 page came back from a reader as 49 words and
        # the batch counted it as read.
        chain.firecrawl = {"url": SHOP, "error": "HTTP 404 (via Firecrawl)", "status_code": 404, "success": False}
        chain.jina = _read("jina_reader", words=49)
        chain.direct = {"url": SHOP, "error": "HTTP 404", "status_code": 404, "text": None}
        result = await ws.scrape_url(None, SHOP)
        assert result["success"] is False
        assert result["status_code"] == 404


class TestJinaTargetErrors:
    """Jina answers 200 for a target that answered 404."""

    def _read(self, monkeypatch, text):
        import asyncio

        import requests

        class _Resp:
            status_code = 200

        _Resp.text = text
        monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: _Resp())
        return asyncio.run(ws.scrape_url_jina(None, SHOP))

    def test_the_targets_404_is_an_error(self, monkeypatch):
        result = self._read(monkeypatch, (
            "Title: File or directory not found.\n\nURL Source: https://shop.hr/x\n\n"
            "Warning: Target URL returned error 404: Not Found\n\nMarkdown Content:\n"
            "# Server Error\n\n## 404 - File or directory not found.\n" + "x " * 60
        ))
        assert result["success"] is False
        assert result["status_code"] == 404

    def test_a_normal_page_reads(self, monkeypatch):
        result = self._read(monkeypatch, (
            "Title: Dizalica\n\nURL Source: https://shop.hr/x\n\nMarkdown Content:\n"
            "Cijena 4.834,75 € " + "riječ " * 80
        ))
        assert result["success"] is True

    def test_a_warning_word_in_the_article_is_not_a_status(self, monkeypatch):
        result = self._read(monkeypatch, (
            "Title: Upozorenje\n\nMarkdown Content:\n"
            "The article quotes: Warning: Target URL returned error 404 in a log. " + "riječ " * 80
        ))
        assert result["success"] is True


class TestAdvancedScraperUsesReadersOnly:
    @pytest.mark.asyncio
    async def test_firecrawl_then_jina_never_direct(self, chain):
        from tools.adk_tools.research_adk_tools import scrape_url_advanced

        chain.firecrawl = _failed("rate limit")
        chain.jina = _failed("down")
        result = await scrape_url_advanced(SHOP)
        assert chain.calls == ["firecrawl", "jina"]
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_an_env_without_readers_still_uses_them(self, chain, monkeypatch):
        from tools.adk_tools.research_adk_tools import scrape_url_advanced

        monkeypatch.setenv("SCRAPE_PROVIDERS", "direct")
        await scrape_url_advanced(SHOP)
        assert chain.calls == ["firecrawl"]


class TestBatchProbing:
    """The HEAD/GET probe is what webshops block: it skipped them before a
    reader, which fetches the page itself, ever saw them."""

    @pytest.mark.asyncio
    async def test_article_batches_are_not_probed_when_a_reader_leads(self, chain, monkeypatch):
        async def probe(url, timeout=5):
            raise AssertionError("must not probe")

        monkeypatch.setattr(ws, "validate_url", probe)
        result = await ws.scrape_multiple_urls(None, [SHOP, SHOP + "?b"])
        assert result["successful"] == 2
        assert result["validated"] == 0

    @pytest.mark.asyncio
    async def test_the_probe_still_runs_when_the_direct_fetch_leads(self, chain, monkeypatch):
        probed = []

        async def probe(url, timeout=5):
            probed.append(url)
            return {"valid": url == SHOP, "status_code": 200 if url == SHOP else 404, "error": "HTTP 404"}

        monkeypatch.setattr(ws, "validate_url", probe)
        monkeypatch.setenv("SCRAPE_PROVIDERS", "direct,jina")
        result = await ws.scrape_multiple_urls(None, [SHOP, SHOP + "/gone"])
        assert len(probed) == 2
        assert result["successful"] == 1
        assert result["failed"] == 1
