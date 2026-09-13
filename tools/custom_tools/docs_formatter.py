"""
Google Docs Formatter - Custom Tool

Konvertira Markdown u Google Docs API batch update zahtjeve.
Omogućava LLM-u da kreira strukturirane i formatirane dokumente bez
direktnog korištenja složenog Docs API-ja.
"""

from typing import Dict, List, Any, Optional
import re
import logging

logger = logging.getLogger(__name__)


def _is_table_separator(line: str) -> bool:
    """The |---|:--:| row that turns two lines into a table."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = _split_row(stripped)
    return bool(cells) and all(
        re.fullmatch(r":?-{1,}:?", c.strip()) for c in cells if c.strip()
    )


def _split_row(line: str) -> List[str]:
    """Cells of one markdown table row, without the outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def split_markdown_blocks(markdown: str) -> List[Dict[str, Any]]:
    """Split markdown into text blocks and table blocks.

    Tables are pulled out because Google Docs has no markdown: a table has
    to be created as a real table element and its cells filled by index.
    Left in the text stream they render as rows of pipe characters, which
    is exactly what a 14,000-character report came out looking like on
    2026-09-06.

    A table needs a header row, a separator row, and at least one body row;
    anything less stays text, because a lone pipe is usually just a pipe.
    """
    lines = markdown.split("\n")
    blocks: List[Dict[str, Any]] = []
    text: List[str] = []
    i = 0

    def flush_text():
        if text:
            blocks.append({"kind": "text", "content": "\n".join(text)})
            text.clear()

    while i < len(lines):
        line = lines[i]
        is_start = (
            i + 2 < len(lines)
            and line.strip().startswith("|")
            and _is_table_separator(lines[i + 1])
            and lines[i + 2].strip().startswith("|")
        )
        if not is_start:
            text.append(line)
            i += 1
            continue

        header = _split_row(line)
        rows = []
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _split_row(lines[j])
            # Pad or trim so every row matches the header width; Docs tables
            # are rectangular and a ragged row would shift every later cell.
            cells = (cells + [""] * len(header))[: len(header)]
            rows.append(cells)
            j += 1

        flush_text()
        blocks.append({"kind": "table", "header": header, "rows": rows})
        i = j

    flush_text()
    return [b for b in blocks if b["kind"] != "text" or b["content"].strip()]


def _units(text: str) -> int:
    """Length in the UTF-16 code units Docs indexes by.

    An emoji outside the BMP counts two; len() counts one and would put every
    later style in the document a character to the left.
    """
    return len(text.encode('utf-16-le')) // 2


# A page break is asked for by name. It used to be a horizontal rule, but the
# synthesizer draws `---` between every chapter out of habit -- the Ex zones
# report had nine of them, one straight under the title -- so a rule is now
# dropped and only this marker breaks the page.
PAGE_BREAK_MARKER = "[[PAGEBREAK]]"
_HORIZONTAL_RULE = re.compile(r'-{3,}|\*{3,}|_{3,}')

_INLINE = re.compile(
    r'`(?P<code>[^`]+)`'
    r'|\*\*(?P<b1>.+?)\*\*'
    r'|__(?P<b2>.+?)__'
    r'|\[(?P<lt>[^\]]+)\]\((?P<lu>[^)\s]+)\)'
    r'|(?<![\w*])\*(?![\s*])(?P<i1>.+?)(?<!\s)\*(?![\w*])'
    r'|(?<!\w)_(?![\s_])(?P<i2>.+?)(?<!\s)_(?!\w)'
)


def strip_inline_markdown(text: str) -> tuple:
    """Plain text plus the styled ranges its markers stood for.

    The markers themselves must not reach the document: the converter used to
    style `**EN 60079-10-2**` bold and still insert both pairs of asterisks, so
    every report carried them. Offsets are UTF-16 units into the returned
    plain text, as Docs counts them.
    Escaped `\\*` and `\\_` come out as the literal character.
    """
    protected = text.replace('\\*', '').replace('\\_', '')
    plain, segments = _strip_inline(protected)
    return plain.replace('', '*').replace('', '_'), segments


def _strip_inline(text: str) -> tuple:
    out: List[str] = []
    segments: List[Dict[str, Any]] = []
    pos = n = 0
    for m in _INLINE.finditer(text):
        before = text[pos:m.start()]
        out.append(before)
        n += _units(before)
        if m.group('code') is not None:
            # Code is shown as written: nothing inside it is markdown.
            inner_plain, inner_segments, seg = m.group('code'), [], {'type': 'code'}
        else:
            if m.group('lt') is not None:
                inner, seg = m.group('lt'), {'type': 'link', 'url': m.group('lu')}
            elif m.group('b1') is not None or m.group('b2') is not None:
                inner, seg = m.group('b1') or m.group('b2'), {'type': 'bold'}
            else:
                inner, seg = m.group('i1') or m.group('i2'), {'type': 'italic'}
            inner_plain, inner_segments = _strip_inline(inner)
        width = _units(inner_plain)
        segments.append({**seg, 'start': n, 'end': n + width})
        segments.extend(
            {**s, 'start': s['start'] + n, 'end': s['end'] + n} for s in inner_segments
        )
        out.append(inner_plain)
        n += width
        pos = m.end()
    out.append(text[pos:])
    return ''.join(out), segments


class DocsFormatter:
    """
    Konverter Markdown -> Google Docs API batch update zahtjevi

    Podržava:
    - Headings (H1-H6)
    - Bold, Italic, Underline
    - Lists (ordered, unordered)
    - Links
    - Paragraphs
    """

    def __init__(self):
        self.requests: List[Dict[str, Any]] = []
        self.current_index = 1  # Docs start index (1-based)

    def markdown_to_docs_requests(self, markdown: str) -> List[Dict[str, Any]]:
        """
        Konvertira Markdown tekst u Google Docs API batch update zahtjeve

        Args:
            markdown: Markdown tekst

        Returns:
            Lista batch update request objekata
        """
        self.requests = []
        self.current_index = 1

        lines = markdown.split('\n')
        # Blank lines right after a heading, a dropped rule or a page break are
        # markdown's separators, not content. Written as empty paragraphs they
        # sit between a heading and its section, and keepWithNext then binds
        # the heading to the blank line instead -- which is how "3.1" and "4."
        # still ended their pages in the Ex zones report.
        swallow_blank = False

        for line in lines:
            line = line.rstrip()

            # Prazan red
            if not line:
                if not swallow_blank:
                    self._add_paragraph("\n")
                continue
            swallow_blank = False

            if line.strip() == PAGE_BREAK_MARKER:
                self._process_page_break()
                swallow_blank = True
            elif _HORIZONTAL_RULE.fullmatch(line.strip()):
                swallow_blank = True
            # Heading
            elif line.startswith('#'):
                self._process_heading(line)
                swallow_blank = True
            # Blockquote: Docs has no quote style, and a literal ">" is what
            # the Ex zones report showed. Keep the text, drop the marker.
            elif line.startswith('>'):
                self._process_paragraph(line.lstrip('>').strip())
            # Unordered list
            elif line.startswith('- ') or line.startswith('* '):
                self._process_list_item(line, ordered=False)
            # Ordered list
            elif re.match(r'^\d+\.\s', line):
                self._process_list_item(line, ordered=True)
            # Paragraph
            else:
                self._process_paragraph(line)

        return self.requests

    def _process_page_break(self) -> None:
        """Start the next content on a new page."""
        self.requests.append({
            'insertPageBreak': {
                'location': {'index': self.current_index}
            }
        })
        # The API inserts the break AND a newline: two index units, not one.
        self.current_index += 2

    def _process_heading(self, line: str) -> None:
        """Procesira heading liniju"""
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if not match:
            return

        level = len(match.group(1))
        text, _ = strip_inline_markdown(match.group(2))

        # Insert text
        self.requests.append({
            'insertText': {
                'location': {'index': self.current_index},
                'text': text + '\n'
            }
        })

        # Apply heading style.
        #
        # keepWithNext is what stops a heading being the last line on a page
        # with its section starting overleaf -- reported 2026-09-06 on
        # chapter 4 of the Ex zones report. Docs solves this properly; the
        # alternative, forcing a page break before every heading, wastes a
        # third of a report in white space.
        #
        # keepLinesTogether covers the other half: a heading long enough to
        # wrap must not split across the break either.
        end_index = self.current_index + _units(text)
        self.requests.append({
            'updateParagraphStyle': {
                'range': {
                    'startIndex': self.current_index,
                    'endIndex': end_index
                },
                'paragraphStyle': {
                    'namedStyleType': f'HEADING_{level}',
                    'keepWithNext': True,
                    'keepLinesTogether': True
                },
                'fields': 'namedStyleType,keepWithNext,keepLinesTogether'
            }
        })

        self.current_index = end_index + 1

    def _process_paragraph(self, line: str) -> None:
        """Procesira obični paragraf s inline formatiranjem"""
        text, segments = strip_inline_markdown(line)

        self.requests.append({
            'insertText': {
                'location': {'index': self.current_index},
                'text': text + '\n'
            }
        })
        self._apply_segments(segments)

        self.current_index += _units(text) + 1

    def _process_list_item(self, line: str, ordered: bool) -> None:
        """Procesira list item"""
        marker = r'^\d+\.\s+' if ordered else r'^[-*]\s+'
        text, segments = strip_inline_markdown(re.sub(marker, '', line))

        # Insert text
        self.requests.append({
            'insertText': {
                'location': {'index': self.current_index},
                'text': text + '\n'
            }
        })

        # Apply list formatting
        end_index = self.current_index + _units(text)
        self.requests.append({
            'createParagraphBullets': {
                'range': {
                    'startIndex': self.current_index,
                    'endIndex': end_index
                },
                'bulletPreset': 'NUMBERED_DECIMAL_ALPHA_ROMAN' if ordered else 'BULLET_DISC_CIRCLE_SQUARE'
            }
        })
        self._apply_segments(segments)

        self.current_index = end_index + 1

    def _add_paragraph(self, text: str) -> None:
        """Dodaje običan paragraf"""
        self.requests.append({
            'insertText': {
                'location': {'index': self.current_index},
                'text': text
            }
        })
        self.current_index += _units(text)

    def _apply_segments(self, segments: List[Dict[str, Any]]) -> None:
        """Style the ranges strip_inline_markdown found, at the current index."""
        for seg in segments:
            if seg['end'] > seg['start']:
                self._apply_text_style(
                    start=self.current_index + seg['start'],
                    end=self.current_index + seg['end'],
                    style_type=seg['type'],
                    url=seg.get('url'),
                )

    def _apply_text_style(self, start: int, end: int, style_type: str,
                          url: Optional[str] = None) -> None:
        """Primjenjuje text style na range"""
        if style_type == 'link' and url:
            self.requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': start, 'endIndex': end},
                    'textStyle': {'link': {'url': url}},
                    'fields': 'link'
                }
            })
        elif style_type == 'code':
            self.requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': start, 'endIndex': end},
                    'textStyle': {'weightedFontFamily': {'fontFamily': 'Roboto Mono'}},
                    'fields': 'weightedFontFamily'
                }
            })
        elif style_type == 'bold':
            self.requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': start, 'endIndex': end},
                    'textStyle': {'bold': True},
                    'fields': 'bold'
                }
            })
        elif style_type == 'italic':
            self.requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': start, 'endIndex': end},
                    'textStyle': {'italic': True},
                    'fields': 'italic'
                }
            })
        elif style_type == 'underline':
            self.requests.append({
                'updateTextStyle': {
                    'range': {'startIndex': start, 'endIndex': end},
                    'textStyle': {'underline': True},
                    'fields': 'underline'
                }
            })


def format_markdown_for_docs(credentials=None, markdown: str = "") -> List[Dict[str, Any]]:
    """
    Helper funkcija za formatiranje Markdown-a za Google Docs

    Args:
        credentials: OAuth credentials (ignored, tool doesn't need auth)
        markdown: Markdown tekst

    Returns:
        Lista batch update zahtjeva za Google Docs API
    """
    formatter = DocsFormatter()
    return formatter.markdown_to_docs_requests(markdown)


# Function declaration za ADK
def get_docs_formatter_tool():
    """
    Vraća Tool sa Docs Formatter FunctionDeclaration

    Returns:
        Tool objekt
    """
    from google.genai.types import Tool, FunctionDeclaration

    return Tool(
        function_declarations=[
            FunctionDeclaration(
                name="format_markdown_for_docs",
                description=(
                    "Convert Markdown text to Google Docs API batch update requests. "
                    "Supports headings, bold, italic, lists, and links. "
                    "Use this to create well-formatted documents instead of plain text."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "markdown": {
                            "type": "string",
                            "description": "Markdown text to convert to Docs format"
                        }
                    },
                    "required": ["markdown"]
                }
            )
        ]
    )
