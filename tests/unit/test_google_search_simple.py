"""
A "simple search" that returns an AI summary leaves the researcher nothing to open.

google_search_simple used to call google_search_grounding and relabel its
citations, so the ReAct plan (grounding -> simple -> scrape) asked the same
summarizer twice and never saw a list of URLs. These tests pin the contract the
researcher's prompt promises: raw hits in, pages to scrape out.

Run with:
    pytest tests/unit/test_google_search_simple.py -v
"""

import pytest

from tools.api_implementations import google_search_api as api


@pytest.fixture
def no_grounding(monkeypatch):
    """Fail loudly if the simple path reaches for the summarizer."""

    async def _boom(**_kwargs):
        raise AssertionError("google_search_simple must not call grounding")

    monkeypatch.setattr(api, "google_search_grounding", _boom)


def _custom_search_returning(sources, error=None):
    """Stub shaped like a provider in the chain (query, num_results, locale)."""
    async def _stub(query, num_results=10, locale=None):
        _stub.calls.append({"query": query, "num_results": num_results, "locale": locale})
        if error:
            return {"query": query, "error": error, "sources": [], "source_count": 0}
        return {
            "query": query,
            "answer": "unused",
            "sources": sources,
            "source_count": len(sources),
            "search_method": "custom_search_api",
        }

    _stub.calls = []
    return _stub


class TestSimpleSearchReturnsRawHits:
    @pytest.mark.asyncio
    async def test_returns_url_title_snippet_per_hit(self, monkeypatch, no_grounding):
        hits = [
            {"url": "https://a.hr/p1", "title": "Dizalica 8 kW", "snippet": "2.499 EUR"},
            {"url": "https://b.hr/p2", "title": "Dizalica 12 kW", "snippet": "3.199 EUR"},
        ]
        stub = _custom_search_returning(hits)
        monkeypatch.setattr(api, "_raw_search_providers", lambda: [("custom_search_api", stub)])

        result = await api.google_search_simple(None, "cijene dizalica topline", num_results=10)

        assert result["results"] == hits
        assert result["result_count"] == 2
        assert result["search_method"] == "custom_search_api"
        for hit in result["results"]:
            assert set(hit) >= {"url", "title", "snippet"}

    @pytest.mark.asyncio
    async def test_query_is_passed_through_unwrapped(self, monkeypatch, no_grounding):
        stub = _custom_search_returning([{"url": "https://a.hr", "title": "A", "snippet": "s"}])
        monkeypatch.setattr(api, "_raw_search_providers", lambda: [("custom_search_api", stub)])

        await api.google_search_simple(None, "cijena bojlera")

        # The old code sent "Find information about: {query}" into a prompt that
        # already prefixed "Search the web and answer:" — double-wrapped noise.
        assert stub.calls[0]["query"] == "cijena bojlera"


class TestGroundingFallbackIsLabelled:
    @pytest.mark.asyncio
    async def test_every_raw_provider_down_falls_back_and_warns(self, monkeypatch):
        """Grounding citations are an AI summary's footnotes, not pages read."""
        async def _dead(query, num_results, locale):
            return {"error": "not configured", "sources": [], "source_count": 0}

        monkeypatch.setattr(
            api, "_raw_search_providers",
            lambda: [("custom_search_api", _dead), ("duckduckgo_search", _dead)],
        )

        async def _grounding(credentials=None, query="", max_results=5, **_kwargs):
            return {"sources": [{"url": "https://c.hr", "title": "C"}], "source_count": 1}

        monkeypatch.setattr(api, "google_search_grounding", _grounding)

        result = await api.google_search_simple(None, "cijene")

        assert result["search_method"] == "vertex_ai_grounding_fallback"
        assert "warning" in result
        assert result["result_count"] == 1


class TestCroatianLocaleSniff:
    @pytest.mark.parametrize(
        "query",
        ["cijene dizalica topline", "koliko kosta bojler", "gdje kupiti pećnicu", "najbolji hladnjak"],
    )
    def test_croatian_queries_detected(self, query):
        assert api._looks_croatian(query) is True

    @pytest.mark.parametrize(
        "query",
        ["heat pump prices 2026", "best air conditioner reviews", "python asyncio tutorial"],
    )
    def test_english_queries_left_alone(self, query):
        assert api._looks_croatian(query) is False

    def test_diacritics_alone_are_enough(self):
        assert api._looks_croatian("štednjak") is True


class TestGroundingSystemInstruction:
    """Grounding used to get a bare 'answer this' with no date, language or format."""

    def test_carries_todays_date(self):
        import datetime as _dt

        instruction = api._grounding_system_instruction("heat pump prices")
        assert _dt.date.today().strftime("%Y-%m-%d") in instruction

    def test_demands_source_and_date_for_every_number(self):
        instruction = api._grounding_system_instruction("heat pump prices").lower()
        assert "source" in instruction
        assert "currency" in instruction
        assert "tax" in instruction

    def test_croatian_query_asks_for_croatian_sources(self):
        assert "Croatian sources" in api._grounding_system_instruction("cijene bojlera")

    def test_english_query_does_not(self):
        assert "Croatian sources" not in api._grounding_system_instruction("boiler prices uk")


class TestCustomSearchCredentialsAreReadPerCall:
    """They were module constants: adding the key to .env changed nothing until
    a restart, and the symptom was a silent fallback rather than an error."""

    @pytest.mark.asyncio
    async def test_a_key_set_after_import_is_picked_up(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "late-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_CX", "late-cx")
        assert api._custom_search_api_key() == "late-key"
        assert api._custom_search_cx() == "late-cx"

    def test_the_dedicated_key_wins_over_the_generic_one(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "gemini-key")
        monkeypatch.setenv("GOOGLE_CUSTOM_SEARCH_API_KEY", "search-key")
        assert api._custom_search_api_key() == "search-key"

    def test_no_key_at_all_reads_as_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert api._custom_search_api_key() is None

    @pytest.mark.asyncio
    async def test_the_missing_key_error_names_what_to_set(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CUSTOM_SEARCH_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        result = await api.google_custom_search("cijene")
        assert "GOOGLE_CUSTOM_SEARCH_API_KEY" in result["error"]
