"""A markdown table has to become a real table, not a row of pipe characters.

Google Docs has no markdown. DocsFormatter handled headings, lists and
paragraphs, and everything else fell through to a plain paragraph -- so a
table written by the synthesizer arrived in the document as "| Zona | Opis |"
lines. Reported 2026-09-06 on the Ex zones document as "tablice su nikakve",
after Opus had spent two minutes writing a perfectly good one.

The dangerous part is not the parsing but the indices: every insert shifts
everything after it, and an off-by-one writes the report into the wrong cells
without failing. So cells are read back from the document and filled from the
LAST index to the first, which nothing before them can move.

Run with:
    pytest tests/unit/test_docs_tables.py -v
"""

import pytest

from tools.adk_tools import docs_adk_tools as dt
from tools.custom_tools.docs_formatter import split_markdown_blocks

MD = """# Klasifikacija

Uvodni tekst o zonama.

| Zona | Prisutnost | Kategorija |
|------|:----------:|-----------:|
| 20 | stalno | 1D |
| 21 | povremeno | 2D |

Zakljucak na kraju.
"""


class TestSplitting:
    def test_the_table_is_lifted_out_of_the_text(self):
        blocks = split_markdown_blocks(MD)
        kinds = [b["kind"] for b in blocks]
        assert kinds == ["text", "table", "text"]

    def test_header_and_rows_are_parsed(self):
        table = [b for b in split_markdown_blocks(MD) if b["kind"] == "table"][0]
        assert table["header"] == ["Zona", "Prisutnost", "Kategorija"]
        assert table["rows"] == [["20", "stalno", "1D"], ["21", "povremeno", "2D"]]

    def test_alignment_markers_do_not_become_a_row(self):
        table = [b for b in split_markdown_blocks(MD) if b["kind"] == "table"][0]
        assert all("---" not in c for row in table["rows"] for c in row)

    def test_a_ragged_row_is_squared_off(self):
        """Docs tables are rectangular; a short row would shift every cell."""
        md = "| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |\n"
        table = split_markdown_blocks(md)[0]
        assert [len(r) for r in table["rows"]] == [3, 3]

    def test_a_lone_pipe_stays_text(self):
        md = "Vrijednost | jedinica su odvojeni crtom.\n"
        assert [b["kind"] for b in split_markdown_blocks(md)] == ["text"]

    def test_a_table_without_body_rows_stays_text(self):
        md = "| a | b |\n|---|---|\n"
        assert [b["kind"] for b in split_markdown_blocks(md)] == ["text"]


class FakeDocs:
    """Enough of the Docs API to watch the index arithmetic."""

    def __init__(self):
        self.batches = []
        self.table_cells = []

    async def get(self, creds, document_id):
        content = [{"endIndex": 100}]
        if self.table_cells:
            rows = []
            for row in self.table_cells:
                rows.append({"tableCells": [{"content": [{"startIndex": i}]} for i in row]})
            content.append({"table": {"tableRows": rows}})
            content.append({"endIndex": 400})
        return {"body": {"content": content}}

    async def batch(self, creds, document_id, requests):
        self.batches.append(requests)
        for r in requests:
            if "insertTable" in r:
                spec = r["insertTable"]
                base = 200
                self.table_cells = [
                    [base + (row * spec["columns"] + col) * 10
                     for col in range(spec["columns"])]
                    for row in range(spec["rows"])
                ]
        return {"status": "ok"}


@pytest.fixture
def fake(monkeypatch):
    api = FakeDocs()
    monkeypatch.setattr(dt, "_get_credentials", lambda: object())
    monkeypatch.setattr(
        "tools.api_implementations.docs_api.docs_get_document", api.get, raising=False
    )
    monkeypatch.setattr(
        "tools.api_implementations.docs_api.docs_batch_update", api.batch, raising=False
    )
    return api


class TestWritingTheTable:
    @pytest.mark.asyncio
    async def test_it_reports_what_it_wrote(self, fake):
        result = await dt.docs_write_markdown("doc-1", MD)
        assert result["status"] == "ok"
        assert result["tables"] == 1
        assert result["blocks"] == 3
        assert result["layout"] == "ok"

    @pytest.mark.asyncio
    async def test_a_real_table_element_is_created(self, fake):
        await dt.docs_write_markdown("doc-1", MD)
        inserts = [r for b in fake.batches for r in b if "insertTable" in r]
        assert len(inserts) == 1
        spec = inserts[0]["insertTable"]
        assert spec["rows"] == 3, "header plus two body rows"
        assert spec["columns"] == 3

    @pytest.mark.asyncio
    async def test_cells_are_filled_from_the_last_index_backwards(self, fake):
        """The whole reason this reads the document back.

        Cell indices in the fake run 200..280; the trailing text block
        lands near 399 and is deliberately excluded here.
        """
        await dt.docs_write_markdown("doc-1", MD)
        fills = [
            r for b in fake.batches for r in b
            if "insertText" in r and 200 <= r["insertText"]["location"]["index"] <= 280
        ]
        indices = [r["insertText"]["location"]["index"] for r in fills]
        assert indices == sorted(indices, reverse=True), (
            "filling forwards would shift every later cell"
        )

    @pytest.mark.asyncio
    async def test_every_cell_gets_its_own_value(self, fake):
        await dt.docs_write_markdown("doc-1", MD)
        written = {
            r["insertText"]["text"]
            for b in fake.batches for r in b
            if "insertText" in r and 200 <= r["insertText"]["location"]["index"] <= 280
        }
        assert written == {
            "Zona", "Prisutnost", "Kategorija",
            "20", "stalno", "1D",
            "21", "povremeno", "2D",
        }

    @pytest.mark.asyncio
    async def test_the_header_row_is_bolded(self, fake):
        await dt.docs_write_markdown("doc-1", MD)
        bolds = [r for b in fake.batches for r in b if "updateTextStyle" in r]
        assert len(bolds) == 3, "one per header cell"
        assert all(r["updateTextStyle"]["textStyle"]["bold"] for r in bolds)

    @pytest.mark.asyncio
    async def test_no_pipe_characters_reach_the_document(self, fake):
        await dt.docs_write_markdown("doc-1", MD)
        texts = [
            r["insertText"]["text"]
            for b in fake.batches for r in b if "insertText" in r
        ]
        assert not any("|" in t for t in texts), "this is the reported bug"


class TestFailuresAreHonest:
    @pytest.mark.asyncio
    async def test_a_lost_answer_is_unknown_not_failed(self, fake, monkeypatch):
        from tools.resilience.retry_handler import UnconfirmedWrite

        async def lose(*a, **kw):
            raise UnconfirmedWrite("connection reset")

        monkeypatch.setattr(
            "tools.api_implementations.docs_api.docs_batch_update", lose, raising=False
        )
        result = await dt.docs_write_markdown("doc-1", MD)
        assert result["outcome"] == "unknown"


class TestHeadingsDoNotGetOrphaned:
    """Reported 2026-09-06: chapter 4's heading was the last line of a page

    with its section starting overleaf. Docs has keepWithNext for exactly
    this; forcing a page break before every heading would instead spend a
    third of the report on white space.
    """

    def _heading_styles(self, markdown):
        from tools.custom_tools.docs_formatter import DocsFormatter

        reqs = DocsFormatter().markdown_to_docs_requests(markdown)
        return [
            r["updateParagraphStyle"] for r in reqs
            if "updateParagraphStyle" in r
            and "HEADING" in r["updateParagraphStyle"]["paragraphStyle"].get(
                "namedStyleType", ""
            )
        ]

    @pytest.mark.parametrize("level", ["#", "##", "###"])
    def test_every_heading_sticks_to_its_section(self, level):
        styles = self._heading_styles(f"{level} Poglavlje\n\nTekst.\n")
        assert styles, "the heading produced no style request at all"
        assert styles[0]["paragraphStyle"]["keepWithNext"] is True

    def test_a_wrapped_heading_does_not_split(self):
        styles = self._heading_styles("# " + "vrlo dug naslov " * 8 + "\n\nX\n")
        assert styles[0]["paragraphStyle"]["keepLinesTogether"] is True

    def test_the_fields_mask_actually_applies_them(self):
        """A property missing from `fields` is silently ignored by Docs."""
        fields = self._heading_styles("# Naslov\n\nX\n")[0]["fields"]
        assert "keepWithNext" in fields
        assert "keepLinesTogether" in fields
        assert "namedStyleType" in fields


def _reqs(markdown):
    from tools.custom_tools.docs_formatter import DocsFormatter

    return DocsFormatter().markdown_to_docs_requests(markdown)


def _texts(reqs):
    return [r["insertText"]["text"] for r in reqs if "insertText" in r]


class TestExplicitPageBreaks:
    """A rule used to break the page. The synthesizer draws one between every
    chapter, so the Ex zones report would have had nine breaks, one straight
    under the title. Only the named marker breaks the page now.
    """

    def test_the_marker_breaks_the_page(self):
        assert any("insertPageBreak" in r for r in _reqs("A\n\n[[PAGEBREAK]]\n\nB\n"))

    @pytest.mark.parametrize("rule", ["---", "***", "___", "-----"])
    def test_a_horizontal_rule_is_dropped(self, rule):
        reqs = _reqs(f"A\n\n{rule}\n\nB\n")
        assert not any("insertPageBreak" in r for r in reqs)
        assert not any(rule in t for t in _texts(reqs)), "nor may it land as text"

    def test_text_after_a_break_starts_two_units_later(self):
        """The API inserts the break AND a newline; counting one unit put every
        later style a character off."""
        reqs = _reqs("[[PAGEBREAK]]\nB\n")
        assert reqs[0]["insertPageBreak"]["location"]["index"] == 1
        assert reqs[1]["insertText"]["location"]["index"] == 3

    def test_a_table_separator_is_not_a_page_break(self):
        """|---|---| lives inside a table and must stay there."""
        blocks = split_markdown_blocks(MD)
        text = "\n".join(b["content"] for b in blocks if b["kind"] == "text")
        assert not any("insertPageBreak" in r for r in _reqs(text))

    def test_ordinary_dashes_in_prose_are_left_alone(self):
        reqs = _reqs("Raspon je 10 - 20 kW.\n")
        assert not any("insertPageBreak" in r for r in reqs)
        assert _texts(reqs)[0] == "Raspon je 10 - 20 kW.\n"


class TestNothingComesBetweenAHeadingAndItsSection:
    """Every heading in the Ex zones report was followed by an empty paragraph,
    so keepWithNext kept the heading with a blank line and "4." still ended
    page 4 with its text overleaf."""

    def test_the_first_thing_after_a_heading_is_its_text(self):
        assert _texts(_reqs("## Poglavlje\n\n\nTekst.\n"))[:2] == ["Poglavlje\n", "Tekst.\n"]

    def test_blank_lines_between_paragraphs_stay(self):
        assert _texts(_reqs("A\n\nB\n"))[:3] == ["A\n", "\n", "B\n"]


class TestInlineMarkersLeaveTheText:
    """The converter styled **EN 60079-10-2** bold and still inserted both pairs
    of asterisks, so every report carried them; list items got no styling."""

    def _one(self, line):
        reqs = _reqs(line)
        text = reqs[0]["insertText"]["text"]
        styles = [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r]
        return text, styles

    def test_bold_markers_go_and_the_words_are_bold(self):
        text, styles = self._one("Norma **EN 60079-10-2** vrijedi.")
        assert text == "Norma EN 60079-10-2 vrijedi.\n"
        (style,) = styles
        rng = style["range"]
        assert text[rng["startIndex"] - 1:rng["endIndex"] - 1] == "EN 60079-10-2"
        assert style["textStyle"] == {"bold": True}

    def test_list_items_get_it_too(self):
        reqs = _reqs("1. **Karakterizacija** — opis")
        assert _texts(reqs)[0] == "Karakterizacija — opis\n"
        assert any("updateTextStyle" in r for r in reqs)

    def test_italic_and_links(self):
        text, styles = self._one("*Verzija 1.0* i [izvor](https://a.hr/x)")
        assert text == "Verzija 1.0 i izvor\n"
        applied = [s["textStyle"] for s in styles]
        assert {"italic": True} in applied
        assert {"link": {"url": "https://a.hr/x"}} in applied

    def test_italic_inside_bold(self):
        text, styles = self._one("**a *b* c**")
        assert text == "a b c\n"
        assert sorted(s["range"]["startIndex"] for s in styles) == [1, 3]

    @pytest.mark.parametrize("line", [
        "2 * 3 * 4 = 24",
        "datoteka file_name_here.txt",
        "Raspon 10 - 20 kW",
    ])
    def test_arithmetic_and_identifiers_are_left_alone(self, line):
        text, styles = self._one(line)
        assert text == line + "\n"
        assert not styles

    def test_an_escaped_asterisk_is_a_literal_one(self):
        text, styles = self._one("\\* Napomena")
        assert text == "* Napomena\n"
        assert not styles

    def test_code_is_monospace_without_backticks(self):
        text, styles = self._one("Oznaka `II 1 D Ex ia` na kućištu")
        assert text == "Oznaka II 1 D Ex ia na kućištu\n"
        (style,) = styles
        assert style["textStyle"] == {"weightedFontFamily": {"fontFamily": "Roboto Mono"}}

    def test_nothing_inside_code_is_markdown(self):
        text, styles = self._one("`a **b** c`")
        assert text == "a **b** c\n"
        assert len(styles) == 1

    def test_an_emoji_counts_two_units_as_docs_does(self):
        """Docs indexes in UTF-16; len() would put the bold one to the left."""
        reqs = _reqs("🎶 **da**\nX")
        (style,) = [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r]
        assert style["range"] == {"startIndex": 4, "endIndex": 6}
        assert reqs[-1]["insertText"]["location"]["index"] == 7

    def test_a_quote_loses_its_marker(self):
        assert _texts(_reqs("> ⚠️ Napomena"))[0] == "⚠️ Napomena\n"


def _para(start, end, text, style="NORMAL_TEXT"):
    return {
        "startIndex": start, "endIndex": end,
        "paragraph": {
            "elements": [{"textRun": {"content": text}}],
            "paragraphStyle": {"namedStyleType": style},
        },
    }


class TestLayoutPass:
    """Run on the real structure after everything is written."""

    DOC = {"body": {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        _para(1, 12, "3.1 Tablica\n", "HEADING_3"),
        _para(12, 13, "\n"),
        {"startIndex": 13, "endIndex": 60, "table": {"rows": 3, "tableRows": [{}, {}, {}]}},
        _para(60, 74, "Nakon tablice\n"),
    ]}}

    def _layout(self):
        return dt._layout_requests(self.DOC)

    def test_the_blank_line_insertTable_leaves_is_glued_once(self):
        glued = [r["updateParagraphStyle"] for r in self._layout() if "updateParagraphStyle" in r]
        assert [g["range"] for g in glued] == [{"startIndex": 12, "endIndex": 13}]
        assert glued[0]["paragraphStyle"] == {"keepWithNext": True}

    def test_tables_repeat_their_header_and_keep_rows_whole(self):
        reqs = self._layout()
        pins = [r["pinTableHeaderRows"] for r in reqs if "pinTableHeaderRows" in r]
        rows = [r["updateTableRowStyle"] for r in reqs if "updateTableRowStyle" in r]
        assert pins == [{"tableStartLocation": {"index": 13}, "pinnedHeaderRowsCount": 1}]
        assert rows[0]["tableRowStyle"] == {"preventOverflow": True}
        assert rows[0]["fields"] == "preventOverflow"

    def test_a_sentence_introducing_the_table_stays_with_it(self):
        doc = {"body": {"content": [
            _para(1, 20, "Sljedece stavke:\n"),
            _para(20, 21, "\n"),
            {"startIndex": 21, "endIndex": 60, "table": {"rows": 2, "tableRows": [{}, {}]}},
        ]}}
        glued = [
            r["updateParagraphStyle"]["range"]["startIndex"]
            for r in dt._layout_requests(doc) if "updateParagraphStyle" in r
        ]
        assert glued == [20, 1]

    def test_the_page_is_a4(self):
        (style,) = [r["updateDocumentStyle"] for r in self._layout() if "updateDocumentStyle" in r]
        size = style["documentStyle"]["pageSize"]
        assert round(size["width"]["magnitude"]) == 595
        assert round(size["height"]["magnitude"]) == 842
        assert style["fields"] == "pageSize"
