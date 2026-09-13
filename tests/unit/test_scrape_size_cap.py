"""
A scraped page is not read once — it is resent on every later call.

In a ReAct loop the tool result stays in the history, so a 60 KB page costs its
tokens again on each of the following calls. Measured 2026-09-03: 1.18M input
tokens in one research turn, mostly page text repeated. A price or a spec lives
in the first few thousand characters; the rest is navigation and footer.

Truncation is announced in the result, so the model knows it is looking at a
slice and can narrow the query instead of concluding the number is absent.

Run with:
    pytest tests/unit/test_scrape_size_cap.py -v
"""

import pytest

from tools.adk_tools.research_adk_tools import _cap_scraped, _scrape_char_limit


class TestCapping:
    def test_a_long_page_is_cut(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "1000")
        result = _cap_scraped({"content": "x" * 50_000, "success": True})

        assert len(result["content"]) <= 1000
        assert result["truncated"] is True
        assert result["original_length"] == 50_000

    def test_a_short_page_is_untouched(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "1000")
        result = _cap_scraped({"content": "cijena 4.199 EUR", "success": True})

        assert result["content"] == "cijena 4.199 EUR"
        assert "truncated" not in result

    def test_the_model_is_told_it_is_a_slice(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "1000")
        result = _cap_scraped({"content": "x" * 5000})
        assert "1000" in result["truncation_note"]
        assert "5000" in result["truncation_note"]

    @pytest.mark.parametrize("field", ["content", "text", "markdown"])
    def test_every_text_field_is_covered(self, field, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "1000")
        assert len(_cap_scraped({field: "x" * 9000})[field]) <= 1000

    def test_batches_are_capped_per_page(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "1000")
        batch = {"results": [{"content": "x" * 9000}, {"content": "y" * 9000}]}
        _cap_scraped(batch)
        assert all(len(r["content"]) <= 1000 for r in batch["results"])


class TestLimitConfig:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("SCRAPE_MAX_CHARS", raising=False)
        assert _scrape_char_limit() == 12000

    def test_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "nonsense")
        assert _scrape_char_limit() == 12000

    def test_an_absurdly_small_limit_is_floored(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_MAX_CHARS", "5")
        assert _scrape_char_limit() == 1000

    def test_non_dict_results_pass_through(self):
        assert _cap_scraped("not a dict") == "not a dict"
