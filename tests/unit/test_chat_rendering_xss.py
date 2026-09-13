"""
Chat messages are rendered as HTML, and their content is not trustworthy.

web/static/index.html renders every message through x-html="formatMessage(...)",
and formatMessage interpolates captured text into src="..." and href="...".
It escaped &, < and > but not quotes — so a URL carrying a quote terminated the
attribute it sat in and the remainder became markup, an event handler included.

The content reaching that function comes from email bodies, scraped pages and
documents, and the chat history is persisted, so this was a stored XSS with the
API token sitting in localStorage next to it.

These are regression guards on the source: the escaping must cover quotes, and
no message HTML may carry a handler assembled from a string.

Run with:
    pytest tests/unit/test_chat_rendering_xss.py -v
"""

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web" / "static"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


class TestEscaping:
    @pytest.mark.parametrize(
        "char,entity",
        [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")],
    )
    def test_every_dangerous_character_is_escaped(self, char, entity):
        assert entity in APP_JS, f"formatMessage must escape {char!r}"

    def test_quotes_are_escaped_before_any_interpolation(self):
        """Escaping after building the HTML would be too late."""
        escape_at = APP_JS.index("&#39;")
        first_src = APP_JS.index('<img src="$')
        assert escape_at < first_src


class TestNoHandlersBuiltFromStrings:
    def test_no_inline_onclick_anywhere(self):
        assert "onclick=" not in APP_JS

    def test_image_zoom_carries_data_not_code(self):
        assert 'data-full-src="$' in APP_JS

    def test_the_behaviour_is_delegated(self):
        assert "img.chat-image-zoom" in APP_JS
        assert "addEventListener('click'" in APP_JS


class TestRenderingSurface:
    def test_messages_are_still_rendered_through_format_message(self):
        """If this stops being true the guards above stop covering anything."""
        assert 'x-html="formatMessage(' in INDEX

    def test_media_urls_stay_restricted_to_known_shapes(self):
        """A javascript: URL must not be able to match the media patterns."""
        media_rules = [
            line for line in APP_JS.splitlines()
            if "[VIDEO:" in line or "[IMAGE:" in line
        ]
        assert media_rules, "media patterns not found — did the renderer move?"
        for line in media_rules:
            if "safe.replace" not in line:
                continue
            assert (
                "/api/media/" in line or "https?" in line or "gs:" in line
            ), f"unrestricted media URL pattern: {line.strip()[:80]}"
