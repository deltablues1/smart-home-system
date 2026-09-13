"""Source adapters for Christian corpus ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (compatible; ChristianCorpusIngest/1.0; "
    "+https://example.local)"
)


@dataclass
class ParsedDocument:
    document_id: str
    title: str
    clean_text: str
    hierarchy: dict[str, Any]


def fetch_url(url: str, timeout: int = 30) -> requests.Response:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    if response.apparent_encoding:
        response.encoding = response.apparent_encoding
    return response


def extract_main_html(soup: BeautifulSoup) -> BeautifulSoup:
    for selector in ("main", "article", "#content", ".content", "body"):
        node = soup.select_one(selector)
        if node is not None:
            return node
    return soup


def clean_whitespace(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+", "/", parsed.path)
    return urlunparse((parsed.scheme.lower() or "https", netloc, path, "", "", ""))


def split_html_sections(source_id: str, html: str) -> list[ParsedDocument]:
    soup = BeautifulSoup(html, "html.parser")
    root = extract_main_html(soup)

    sections: list[ParsedDocument] = []
    current_title = "Introduction"
    current_blocks: list[str] = []
    current_index = 1

    def flush() -> None:
        nonlocal current_blocks, current_index
        clean_text = clean_whitespace("\n\n".join(current_blocks))
        if not clean_text:
            current_blocks = []
            return
        sections.append(
            ParsedDocument(
                document_id=f"{source_id}_sec_{current_index:03d}",
                title=current_title,
                clean_text=clean_text,
                hierarchy={"section": current_title},
            )
        )
        current_blocks = []
        current_index += 1

    for element in root.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = clean_whitespace(element.get_text(" ", strip=True))
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            flush()
            current_title = text
        else:
            current_blocks.append(text)

    flush()

    if not sections:
        full_text = clean_whitespace(root.get_text("\n", strip=True))
        if full_text:
            sections.append(
                ParsedDocument(
                    document_id=f"{source_id}_sec_001",
                    title="Full Text",
                    clean_text=full_text,
                    hierarchy={"section": "Full Text"},
                )
            )

    return sections


def html_stem_name(url: str) -> str:
    path = urlparse(url).path.rsplit("/", 1)[-1]
    return re.sub(r"\.html?$", "", path, flags=re.IGNORECASE)


def build_ccel_toc_url(url: str) -> str:
    if url.lower().endswith(".toc.html"):
        return url
    return re.sub(r"\.html?$", ".toc.html", url, flags=re.IGNORECASE)


def collect_ccel_page_links(toc_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    toc_base = toc_url.rsplit("/", 1)[0] + "/"
    prefix = html_stem_name(toc_url).replace(".toc", "")

    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(toc_base, href)
        filename = absolute.rsplit("/", 1)[-1]
        if not filename.lower().endswith(".html"):
            continue
        if ".toc." in filename.lower():
            continue
        if not filename.lower().startswith(prefix.lower()):
            continue
        links.append(normalize_url(absolute))
    return dedupe_preserve_order(links)


def is_probable_ccel_toc(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chapter_lines = sum(
        1 for line in lines[:120] if re.match(r"^(Book|Chapter)\b", line, re.IGNORECASE)
    )
    return chapter_lines >= 12


def extract_ccel_document(url: str, html: str, source_id: str) -> ParsedDocument | None:
    soup = BeautifulSoup(html, "html.parser")
    text_node = soup.select_one("#theText")
    if text_node is None:
        text_node = soup.select_one("#book-section")
    if text_node is None:
        return None

    raw_text = clean_whitespace(text_node.get_text("\n", strip=True))
    if not raw_text:
        return None

    if is_probable_ccel_toc(raw_text):
        return None

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    filtered: list[str] = []
    for line in lines:
        low = line.lower()
        if low in {
            "contents loading…",
            "contents loading...",
            "contents",
            "confessions of saint augustine",
        }:
            continue
        if "theme font" in low or "bible version" in low or "reader width" in low:
            continue
        if "prev" in low or "next" in low:
            continue
        if line in {"Previous", "Next", "-"}:
            continue
        filtered.append(line)

    if not filtered:
        return None

    heading_candidates = [
        clean_whitespace(h.get_text(" ", strip=True))
        for h in soup.find_all(["h1", "h2", "h3"])
    ]
    heading_candidates = [
        h for h in heading_candidates if h and h.lower() != "contents"
    ]
    title = heading_candidates[-1] if heading_candidates else filtered[0]
    if title.lower() == "contents" and len(filtered) > 1:
        title = filtered[1]

    while filtered and filtered[0].strip().lower() == title.strip().lower():
        filtered.pop(0)
    while filtered and filtered[-1].strip().lower() == title.strip().lower():
        filtered.pop()

    clean_text = clean_whitespace("\n".join(filtered))
    if len(clean_text) < 250:
        return None

    page_name = html_stem_name(url)
    return ParsedDocument(
        document_id=f"{source_id}_{page_name}",
        title=title[:180],
        clean_text=clean_text,
        hierarchy={"section": title[:180], "page": page_name},
    )


def strip_gutenberg_boilerplate(text: str) -> str:
    start_markers = [
        "*** START OF THE PROJECT GUTENBERG EBOOK",
        "*** START OF THIS PROJECT GUTENBERG EBOOK",
    ]
    end_markers = [
        "*** END OF THE PROJECT GUTENBERG EBOOK",
        "*** END OF THIS PROJECT GUTENBERG EBOOK",
    ]

    for marker in start_markers:
        idx = text.find(marker)
        if idx != -1:
            text = text[text.find("\n", idx) + 1 :]
            break

    for marker in end_markers:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
            break

    return clean_whitespace(text)


def split_plaintext_sections(source_id: str, text: str) -> list[ParsedDocument]:
    heading_pattern = re.compile(
        r"^(book|chapter|part|section)\b.*$",
        re.IGNORECASE,
    )
    sections: list[ParsedDocument] = []
    current_title = "Introduction"
    current_blocks: list[str] = []
    current_index = 1

    def flush() -> None:
        nonlocal current_blocks, current_index
        clean_text = clean_whitespace("\n\n".join(current_blocks))
        if not clean_text:
            current_blocks = []
            return
        sections.append(
            ParsedDocument(
                document_id=f"{source_id}_sec_{current_index:03d}",
                title=current_title,
                clean_text=clean_text,
                hierarchy={"section": current_title},
            )
        )
        current_blocks = []
        current_index += 1

    for raw_line in text.splitlines():
        line = clean_whitespace(raw_line)
        if not line:
            continue
        if heading_pattern.match(line) and len(line) < 120:
            flush()
            current_title = line
        else:
            current_blocks.append(line)

    flush()

    return sections


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug.lower() or "section"


def strip_bible_front_matter(text: str) -> str:
    text = strip_gutenberg_boilerplate(text)
    marker = "Genesis Chapter 1"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx:]
    return clean_whitespace(text)


def flush_bible_verse_group(
    documents: list[ParsedDocument],
    source_id: str,
    book: str,
    chapter: int,
    verses: list[tuple[int, str]],
    intro_lines: list[str],
) -> None:
    if not verses:
        return

    verse_start = verses[0][0]
    verse_end = verses[-1][0]
    if verse_start == verse_end:
        verse_label = f"{book} {chapter}:{verse_start}"
    else:
        verse_label = f"{book} {chapter}:{verse_start}-{verse_end}"

    lines = [verse_label]
    if intro_lines:
        lines.extend(intro_lines)
    for verse_no, verse_text in verses:
        lines.append(f"{chapter}:{verse_no}. {verse_text}")

    documents.append(
        ParsedDocument(
            document_id=(
                f"{source_id}_{slugify(book)}_{chapter:03d}_"
                f"{verse_start:03d}_{verse_end:03d}"
            ),
            title=verse_label,
            clean_text=clean_whitespace("\n".join(lines)),
            hierarchy={
                "book": book,
                "chapter": str(chapter),
                "verse_start": verse_start,
                "verse_end": verse_end,
            },
        )
    )


def parse_bible_text(source_id: str, text: str) -> list[ParsedDocument]:
    chapter_re = re.compile(r"^(.+?) Chapter (\d+)$")
    verse_re = re.compile(r"^(\d+):(\d+)\.\s*(.*)$")

    documents: list[ParsedDocument] = []
    current_book: str | None = None
    current_chapter: int | None = None
    current_intro_lines: list[str] = []
    current_group_intro: list[str] = []
    current_group: list[tuple[int, str]] = []
    current_verse_no: int | None = None
    current_verse_text: list[str] = []

    def finish_current_verse() -> None:
        nonlocal current_verse_no, current_verse_text
        if current_verse_no is None:
            return
        verse_text = clean_whitespace(" ".join(current_verse_text))
        current_group.append((current_verse_no, verse_text))
        current_verse_no = None
        current_verse_text = []

    def flush_group(force: bool = False) -> None:
        nonlocal current_group, current_group_intro
        if current_book is None or current_chapter is None:
            current_group = []
            current_group_intro = []
            return
        if not current_group:
            current_group_intro = []
            return
        if not force and len(current_group) < 6:
            return
        flush_bible_verse_group(
            documents=documents,
            source_id=source_id,
            book=current_book,
            chapter=current_chapter,
            verses=current_group,
            intro_lines=current_group_intro,
        )
        current_group = []
        current_group_intro = []

    def flush_chapter() -> None:
        finish_current_verse()
        if current_group and current_book is not None and current_chapter is not None:
            flush_bible_verse_group(
                documents=documents,
                source_id=source_id,
                book=current_book,
                chapter=current_chapter,
                verses=current_group,
                intro_lines=current_group_intro,
            )

    for raw_line in text.splitlines():
        line = clean_whitespace(raw_line)
        if not line:
            continue

        chapter_match = chapter_re.match(line)
        if chapter_match:
            flush_chapter()
            current_book = chapter_match.group(1).strip()
            current_chapter = int(chapter_match.group(2))
            current_intro_lines = []
            current_group_intro = []
            current_group = []
            current_verse_no = None
            current_verse_text = []
            continue

        if current_book is None or current_chapter is None:
            continue

        verse_match = verse_re.match(line)
        if verse_match:
            finish_current_verse()
            flush_group(force=False)

            verse_chapter = int(verse_match.group(1))
            verse_no = int(verse_match.group(2))
            verse_text = verse_match.group(3).strip()

            if verse_chapter != current_chapter:
                flush_chapter()
                current_chapter = verse_chapter
                current_intro_lines = []
                current_group_intro = []
                current_group = []

            if not current_group:
                current_group_intro = list(current_intro_lines)
            current_verse_no = verse_no
            current_verse_text = [verse_text]
            continue

        if current_verse_no is not None:
            current_verse_text.append(line)
        else:
            current_intro_lines.append(line)

    flush_chapter()
    return documents


def normalize_vatican_text_lines(text: str) -> list[str]:
    lines = [clean_whitespace(line) for line in text.splitlines()]
    filtered: list[str] = []
    for line in lines:
        if not line:
            continue
        low = line.lower()
        if low in {
            "help",
            "intratext - text",
            "previous",
            "next",
            "-",
            "previous -",
        }:
            continue
        if "catechism of the catholic church" in low and len(line) < 80:
            continue
        if "copyright" in low and "libreria editrice vaticana" in low:
            continue
        filtered.append(line)
    return filtered


def dedupe_adjacent_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    previous = None
    for line in lines:
        if line == previous:
            continue
        result.append(line)
        previous = line
    return result


def fetch_vatican_paginated(entry: dict[str, Any], base_url: str, index_html: str) -> list[ParsedDocument]:
    soup = BeautifulSoup(index_html, "html.parser")
    hrefs = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if re.fullmatch(r"__P[0-9A-Z]+\.HTM", href, flags=re.IGNORECASE):
            hrefs.append(urljoin(base_url, href))

    pages = dedupe_preserve_order(hrefs)
    documents: list[ParsedDocument] = []
    for idx, page_url in enumerate(pages, start=1):
        html = fetch_url(page_url).text
        page_soup = BeautifulSoup(html, "html.parser")
        body = page_soup.body
        if body is None:
            continue
        lines = normalize_vatican_text_lines(body.get_text("\n", strip=True))
        lines = dedupe_adjacent_lines(lines)
        if not lines:
            continue

        title_parts: list[str] = []
        content_start = 0
        for i, line in enumerate(lines):
            if re.match(r"^\d+[A-Za-z]?\s", line):
                content_start = i
                break
            title_parts.append(line)
            if len(title_parts) >= 3:
                content_start = i + 1
                break

        if len(title_parts) >= 2 and title_parts[1].startswith('"'):
            title_parts = title_parts[:1]
            content_start = 1

        normalized_title_parts: list[str] = []
        for part in title_parts:
            part = part.strip(" -")
            if not part:
                continue
            if normalized_title_parts and part == normalized_title_parts[-1]:
                continue
            if normalized_title_parts and normalized_title_parts[-1].endswith(part):
                continue
            normalized_title_parts.append(part)

        title = " / ".join(normalized_title_parts[:2]).strip() or f"Section {idx}"
        content_lines = lines[content_start:] if content_start < len(lines) else lines
        clean_text = clean_whitespace("\n".join(content_lines))
        if len(clean_text) < 80:
            continue

        documents.append(
            ParsedDocument(
                document_id=f"{entry['source_id']}_p_{idx:03d}",
                title=title[:180],
                clean_text=clean_text,
                hierarchy={"section": title[:180], "page": idx},
            )
        )
    return documents


def split_vatican_single_page(entry: dict[str, Any], html: str) -> list[ParsedDocument]:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if body is None:
        return []

    blocks: list[tuple[str, str]] = []
    current_title = "Introduction"
    current_lines: list[str] = []
    current_idx = 1

    def flush() -> None:
        nonlocal current_lines, current_idx
        clean_text = clean_whitespace("\n".join(current_lines))
        if len(clean_text) < 120:
            current_lines = []
            return
        blocks.append((current_title, clean_text))
        current_lines = []
        current_idx += 1

    for node in body.descendants:
        if getattr(node, "name", None) == "a":
            anchor_name = (node.get("name") or "").strip()
            if anchor_name and not anchor_name.startswith("_ftn"):
                title_text = clean_whitespace(node.get_text(" ", strip=True))
                if title_text and len(title_text) < 160:
                    flush()
                    current_title = title_text
                    continue
        if getattr(node, "name", None) == "p":
            text = clean_whitespace(node.get_text(" ", strip=True))
            if not text:
                continue
            low = text.lower()
            if low.startswith("[ be , de , en"):
                continue
            if "libreria editrice vaticana" in low and "copyright" in low:
                continue
            current_lines.append(text)

    flush()

    if not blocks:
        full_text = clean_whitespace(body.get_text("\n", strip=True))
        if full_text:
            blocks = [("Introduction", full_text)]

    documents: list[ParsedDocument] = []
    for idx, (title, clean_text) in enumerate(blocks, start=1):
        documents.append(
            ParsedDocument(
                document_id=f"{entry['source_id']}_sec_{idx:03d}",
                title=title[:180],
                clean_text=clean_text,
                hierarchy={"section": title[:180]},
            )
        )
    return documents


def fetch_ccel(entry: dict[str, Any]) -> list[ParsedDocument]:
    toc_url = build_ccel_toc_url(entry["canonical_url"])
    toc_html = fetch_url(toc_url).text
    page_links = collect_ccel_page_links(toc_url, toc_html)

    documents: list[ParsedDocument] = []
    for page_url in page_links:
        parsed = extract_ccel_document(
            url=page_url,
            html=fetch_url(page_url).text,
            source_id=entry["source_id"],
        )
        if parsed is not None:
            documents.append(parsed)
    return documents


def fetch_vatican(entry: dict[str, Any]) -> list[ParsedDocument]:
    response = fetch_url(entry["canonical_url"])
    html = response.text
    if re.search(r"__P[0-9A-Z]+\.HTM", html, flags=re.IGNORECASE):
        return fetch_vatican_paginated(entry, response.url, html)
    return split_vatican_single_page(entry, html)


_PDF_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_PDF_PARAGRAPH_MARKER_RE = re.compile(r"^\d{1,3}\.\s+")


def _pdf_font_tounicode_map(font_obj: Any) -> dict[int, str]:
    """Parses a font's /ToUnicode CMap directly.

    pypdf/pdfplumber/pymupdf all mis-decode this document's fonts (they emit
    U+FFFD for common Croatian diacritics) even though the embedded ToUnicode
    CMaps are complete and correct — verified by comparing against this
    direct regex parse. Root cause looks like those libraries' CMap parsers
    choking on the ligature bfchar entries (f_l/T_h/f_i mapping one code to
    a 2-character value) that precede the diacritic entries in this font.
    """
    tounicode = font_obj.get("/ToUnicode")
    if tounicode is None:
        return {}
    data = tounicode.get_object().get_data().decode("latin-1", errors="replace")
    mapping: dict[int, str] = {}
    for match in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", data):
        code = int(match.group(1), 16)
        value = match.group(2)
        mapping[code] = "".join(
            chr(int(value[i : i + 4], 16)) for i in range(0, len(value), 4)
        )
    return mapping


def _pdf_font_byte_width(font_obj: Any) -> int:
    return 2 if str(font_obj.get("/Subtype")) == "/Type0" else 1


def _extract_pdf_page_text(page: Any, reader: Any) -> str:
    """Reconstructs page text from raw content-stream operators using our
    own verified CMap decode instead of the (broken, for this document)
    library text extraction. See _pdf_font_tounicode_map for why."""
    from pypdf.generic import ContentStream

    try:
        fonts = page["/Resources"]["/Font"]
    except KeyError:
        return ""

    font_maps: dict[str, dict[int, str]] = {}
    font_widths: dict[str, int] = {}
    for name, font_ref in fonts.items():
        font_obj = font_ref.get_object()
        font_maps[name] = _pdf_font_tounicode_map(font_obj)
        font_widths[name] = _pdf_font_byte_width(font_obj)

    try:
        content_stream = ContentStream(page["/Contents"], reader)
    except Exception:
        return ""

    current_font: str | None = None
    lines: list[str] = []
    current_line: list[str] = []

    def flush_line() -> None:
        if current_line:
            lines.append("".join(current_line))
            current_line.clear()

    for operands, operator in content_stream.operations:
        if operator == b"Tf":
            current_font = str(operands[0])
        elif operator in (b"Tj", b"TJ"):
            items = operands[0] if operator == b"TJ" else [operands[0]]
            font_map = font_maps.get(current_font, {})
            width = font_widths.get(current_font, 1)
            for item in items:
                raw = getattr(item, "_original_bytes", None)
                if raw is not None:
                    for i in range(0, len(raw) - width + 1, width):
                        code = (raw[i] << 8 | raw[i + 1]) if width == 2 else raw[i]
                        if code == 0:
                            continue
                        current_line.append(font_map.get(code, "�"))
                elif isinstance(item, (int, float)) and item < -100:
                    current_line.append(" ")
        elif operator in (b"T*", b"Td", b"TD", b"Tm", b"'", b'"'):
            flush_line()

    flush_line()
    return "\n".join(lines)


def _clean_pdf_page_text(raw_text: str) -> str:
    """Joins wrapped lines into paragraphs, de-hyphenates line-break splits,
    drops running page-number lines, and starts a new paragraph at numbered
    markers (e.g. "117.") typical of papal documents."""
    text = ""
    for raw_line in raw_text.split("\n"):
        stripped = raw_line.strip()
        if not stripped or _PDF_PAGE_NUMBER_RE.match(stripped):
            continue
        if text.endswith("-"):
            text = text[:-1] + stripped
        elif not text:
            text = stripped
        elif _PDF_PARAGRAPH_MARKER_RE.match(stripped):
            text += "\n\n" + stripped
        else:
            text += " " + stripped
    return clean_whitespace(text)


def fetch_local_pdf(entry: dict[str, Any]) -> list[ParsedDocument]:
    """Ingests a local PDF (e.g. an encyclical) page by page.

    Unlike the other adapters this reads from entry["local_path"] instead of
    fetching entry["canonical_url"].
    """
    from pypdf import PdfReader

    local_path = entry.get("local_path")
    if not local_path:
        raise ValueError(
            f"local_pdf adapter requires 'local_path' for source {entry['source_id']}"
        )

    reader = PdfReader(local_path)
    documents: list[ParsedDocument] = []
    for page_num, page in enumerate(reader.pages, start=1):
        raw_text = _extract_pdf_page_text(page, reader)
        clean_text = _clean_pdf_page_text(raw_text)
        if len(clean_text) < 80:
            continue
        documents.append(
            ParsedDocument(
                document_id=f"{entry['source_id']}_p_{page_num:03d}",
                title=f"{entry['work_title']}, str. {page_num}",
                clean_text=clean_text,
                hierarchy={"page": page_num},
            )
        )
    return documents


def fetch_gutenberg(entry: dict[str, Any]) -> list[ParsedDocument]:
    response = fetch_url(entry["canonical_url"])
    content_type = response.headers.get("content-type", "").lower()
    text = response.text

    if "html" in content_type or "<html" in text.lower():
        return split_html_sections(entry["source_id"], text)

    clean_text = strip_gutenberg_boilerplate(text)
    return split_plaintext_sections(entry["source_id"], clean_text)


def fetch_bible_gutenberg(entry: dict[str, Any]) -> list[ParsedDocument]:
    response = fetch_url(entry["canonical_url"], timeout=60)
    clean_text = strip_bible_front_matter(response.text)
    return parse_bible_text(entry["source_id"], clean_text)


ADAPTERS = {
    "bible_gutenberg": fetch_bible_gutenberg,
    "ccel": fetch_ccel,
    "gutenberg": fetch_gutenberg,
    "vatican": fetch_vatican,
    "local_pdf": fetch_local_pdf,
}
