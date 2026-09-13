"""
Docs ADK Tools

ADK-compatible wrappers for Google Docs operations:
- Create and manage documents
- Insert and format text
- Batch updates for complex formatting
- Export documents

Includes Markdown-to-Docs formatter for structured content creation.
"""

import logging
from typing import List, Dict, Any, Optional

from tools.resilience.retry_handler import UnconfirmedWrite
logger = logging.getLogger(__name__)


def _get_credentials():
    """Get OAuth credentials from token file"""
    try:
        from auth.oauth_manager import get_oauth_manager
        oauth_manager = get_oauth_manager()
        creds = oauth_manager.get_credentials()
        if creds and creds.valid:
            return creds
        logger.warning("No valid credentials available for Docs operations")
        return None
    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


async def docs_create_document(
    title: str,
    content: Optional[str] = None,
    share: bool = False,
    share_role: str = "reader",
) -> dict:
    """
    Create a new Google Docs document, fill it with content, and optionally share it.

    This is the ONE-CALL way to produce a finished document. When `content` is
    provided it is written into the document (Markdown is auto-formatted into
    headings/bold/lists; on any formatting issue it falls back to plain text).
    When `share=True` the document is shared (anyone with the link can view),
    so emailed links work without an "access denied" error.

    Prefer this single call over create-then-batch_update: it guarantees the
    document is never left empty and the returned URL is immediately usable.

    Args:
        title: Document title
        content: Document body. Plain text or Markdown
            (# headings, **bold**, - lists, [text](url)). Strongly recommended.
        share: If True, share the document as "anyone with link" (default False).
            Note: when a created document is emailed, the mailer auto-shares it
            with the actual recipient (least privilege), so public link-sharing
            is normally unnecessary.
        share_role: Permission when sharing: "reader" (default), "commenter", or "writer".

    Returns:
        Dictionary with document_id, title, document_url, content_inserted,
        shared (bool), and status.
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.docs_api import (
            docs_create_document as docs_create_impl,
            docs_batch_update as docs_batch_impl,
            docs_insert_text as docs_insert_impl,
        )

        # 1. Create the (empty) document first; we insert content ourselves so
        #    we can apply Markdown formatting.
        result = await docs_create_impl(creds, title, None)
        if not isinstance(result, dict) or result.get("error") or not result.get("document_id"):
            return result
        doc_id = result["document_id"]

        # 2. Insert content (Markdown-formatted, with plain-text fallback).
        content_inserted = False
        if content and content.strip():
            try:
                try:
                    from tools.custom_tools.docs_formatter import DocsFormatter
                    requests = DocsFormatter().markdown_to_docs_requests(content)
                    if requests:
                        await docs_batch_impl(creds, doc_id, requests)
                        content_inserted = True
                except Exception as fmt_err:
                    logger.warning(f"Markdown formatting failed, falling back to plain text: {fmt_err}")
                if not content_inserted:
                    await docs_insert_impl(creds, doc_id, content, 1)
                    content_inserted = True
            except Exception as insert_err:
                # Both insert paths failed — don't leave an empty orphan doc behind.
                try:
                    from tools.api_implementations.drive_api import (
                        drive_delete_file as drive_trash_impl,
                    )
                    await drive_trash_impl(creds, doc_id)
                    logger.error(
                        f"Content insertion failed for '{title}' ({doc_id}); "
                        f"empty document moved to trash: {insert_err}"
                    )
                    return {
                        "error": f"Content insertion failed: {insert_err}. "
                                 "The empty document was moved to trash.",
                        "title": title,
                        "status": "error",
                        "cleaned_up": True,
                    }
                except Exception as cleanup_err:
                    logger.error(
                        f"Content insertion failed AND cleanup failed for {doc_id}: "
                        f"{insert_err} / {cleanup_err}"
                    )
                    result["status"] = "partial"
                    result["content_inserted"] = False
                    result["error"] = (
                        f"Content insertion failed: {insert_err}. An EMPTY document "
                        "remains (cleanup also failed) — inform the user."
                    )
                    return result
        result["content_inserted"] = content_inserted
        if content_inserted:
            result["layout"] = await _apply_layout(creds, doc_id)

        # 3. Optionally share so links are accessible to recipients.
        if share:
            try:
                from tools.api_implementations.drive_api import drive_share_file as drive_share_impl
                share_res = await drive_share_impl(creds, doc_id, None, share_role, "anyone")
                result["shared"] = not (isinstance(share_res, dict) and share_res.get("error"))
                result["share_role"] = share_role
            except Exception as share_err:
                logger.error(f"Sharing failed for {doc_id}: {share_err}")
                result["shared"] = False
                result["share_error"] = str(share_err)

        return result
    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in docs_create_document: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Document creation failed: {e}")
        return {"error": str(e), "title": title}


async def docs_get_document(document_id: str, include_suggestions: bool = False) -> dict:
    """
    Get the content and metadata of a Google Docs document.

    Retrieves document title, text content, structure, and formatting information.
    Useful for reading existing documents before updating them.

    Args:
        document_id: Google Docs document ID (from URL)
        include_suggestions: Include suggestions mode changes (default: False)

    Returns:
        Dictionary with document_id, title, text_content, body structure, and metadata
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.docs_api import docs_get_document as docs_get_impl

        result = await docs_get_impl(creds, document_id, include_suggestions)
        return result
    except Exception as e:
        logger.error(f"Failed to get document {document_id}: {e}")
        return {"error": str(e), "document_id": document_id}


async def docs_insert_text(document_id: str, text: str, index: int = 1) -> dict:
    """
    Insert text at a specific location in the document.

    Inserts plain text at the specified index position. For formatted text,
    use docs_batch_update with format_markdown_for_docs instead.

    Args:
        document_id: Document ID
        text: Text to insert
        index: Position to insert (1-based, default: 1 for beginning)

    Returns:
        Dictionary with document_id, inserted_text_length, and status
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.docs_api import docs_insert_text as docs_insert_impl

        result = await docs_insert_impl(creds, document_id, text, index)
        return result
    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in docs_insert_text: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Failed to insert text in {document_id}: {e}")
        return {"error": str(e), "document_id": document_id}


async def docs_batch_update(document_id: str, requests: List[Dict[str, Any]]) -> dict:
    """
    Perform batch updates for complex formatting operations.

    Use this for applying multiple formatting operations efficiently:
    - Headings (H1-H6)
    - Bold, italic, underline
    - Lists (ordered, unordered)
    - Paragraph styles
    - Links

    Best practice: Use format_markdown_for_docs to generate requests array
    from Markdown, then pass to this function.

    Args:
        document_id: Document ID
        requests: Array of Docs API batch update request objects

    Returns:
        Dictionary with document_id, updates_applied_count, and status
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.docs_api import docs_batch_update as docs_batch_impl

        result = await docs_batch_impl(creds, document_id, requests)
        return result
    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in docs_batch_update: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Batch update failed for {document_id}: {e}")
        return {"error": str(e), "document_id": document_id}


async def docs_format_text(
    document_id: str,
    start_index: int,
    end_index: int,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    font_size: Optional[int] = None
) -> dict:
    """
    Apply text formatting to a specific range in the document.

    Use this for simple formatting operations on existing text.
    For complex formatting, use docs_batch_update instead.

    Args:
        document_id: Document ID
        start_index: Start position (1-based)
        end_index: End position (1-based)
        bold: Apply bold formatting (optional)
        italic: Apply italic formatting (optional)
        underline: Apply underline formatting (optional)
        font_size: Set font size in points (optional)

    Returns:
        Dictionary with document_id, formatted_range, and status
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    try:
        from tools.api_implementations.docs_api import docs_format_text as docs_format_impl

        result = await docs_format_impl(
            creds, document_id, start_index, end_index,
            bold, italic, underline, font_size
        )
        return result
    except Exception as e:
        logger.error(f"Text formatting failed for {document_id}: {e}")
        return {"error": str(e), "document_id": document_id}


async def format_markdown_for_docs(markdown: str) -> dict:
    """
    Convert Markdown to Google Docs API batch update requests.

    This is a helper tool that converts Markdown syntax to Docs API format.
    Use it to prepare formatted content for docs_batch_update.

    Supported Markdown:
    - Headings: # H1, ## H2, ### H3, etc.
    - Bold: **text** or __text__
    - Italic: *text* or _text_
    - Lists: - item or * item (unordered), 1. item (ordered)
    - Links: [text](url)
    - Paragraphs: Plain text

    Workflow:
    1. Compose content in Markdown
    2. Call format_markdown_for_docs(markdown)
    3. Pass resulting requests to docs_batch_update(document_id, requests)

    Args:
        markdown: Markdown text to convert

    Returns:
        Dictionary with requests array and formatted_length
    """
    try:
        from tools.custom_tools.docs_formatter import DocsFormatter

        formatter = DocsFormatter()
        requests = formatter.markdown_to_docs_requests(markdown)

        return {
            "requests": requests,
            "formatted_length": formatter.current_index - 1,
            "markdown_length": len(markdown),
            "status": "formatted"
        }
    except Exception as e:
        logger.error(f"Markdown formatting failed: {e}")
        return {"error": str(e), "markdown_length": len(markdown)}


async def docs_write_markdown(document_id: str, markdown: str) -> dict:
    """Napisi Markdown u dokument, s PRAVIM tablicama.

    Koristi OVO umjesto format_markdown_for_docs + docs_batch_update kad
    sadrzaj ima tablicu. Google Docs ne razumije Markdown: tablica ostavljena
    u tekstu zavrsi kao niz redaka s uspravnim crtama.

    Args:
        document_id: ID dokumenta
        markdown: Markdown sadrzaj (naslovi, liste, podebljano, tablice)

    Returns:
        Broj upisanih blokova i tablica, ili greska.
    """
    creds = _get_credentials()
    if creds is None:
        return {"error": "Authentication required"}

    from tools.custom_tools.docs_formatter import DocsFormatter, split_markdown_blocks
    from tools.api_implementations.docs_api import (
        docs_batch_update as _batch,
        docs_get_document as _get,
    )

    async def end_index() -> int:
        """Where the next append goes: before the body's final newline."""
        doc = await _get(creds, document_id)
        content = (doc.get("body") or {}).get("content") or []
        return max(1, (content[-1].get("endIndex", 2) if content else 2) - 1)

    try:
        blocks = split_markdown_blocks(markdown)
        tables_written = 0

        for block in blocks:
            if block["kind"] == "text":
                start = await end_index()
                formatter = DocsFormatter()
                formatter.current_index = start
                requests = formatter.markdown_to_docs_requests(block["content"])
                # The formatter resets current_index to 1 on entry, so re-apply
                # the offset it should have started from.
                if start != 1:
                    requests = _shift_requests(requests, start - 1)
                if requests:
                    await _batch(creds, document_id, requests)
                continue

            header, rows = block["header"], block["rows"]
            at = await end_index()
            await _batch(creds, document_id, [{
                "insertTable": {
                    "location": {"index": at},
                    "rows": len(rows) + 1,
                    "columns": len(header),
                }
            }])

            # Read the real cell indices back. Computing them is possible but
            # every insert shifts what follows, and a silent off-by-one writes
            # the report into the wrong cells.
            cells = _last_table_cells(await _get(creds, document_id))
            values = [header] + rows
            fills = []
            for (r, c, index) in cells:
                if r < len(values) and c < len(values[r]):
                    text = values[r][c]
                    if text:
                        fills.append((index, text))

            # Backwards: an insert never moves anything before it.
            fills.sort(key=lambda pair: pair[0], reverse=True)
            if fills:
                await _batch(creds, document_id, [
                    {"insertText": {"location": {"index": i}, "text": t}}
                    for i, t in fills
                ])

            # Header bold, against freshly read indices.
            head = _last_table_cells(await _get(creds, document_id))
            bolds = []
            for (r, c, index) in head:
                if r == 0 and c < len(header) and header[c]:
                    bolds.append({
                        "updateTextStyle": {
                            "range": {
                                "startIndex": index,
                                "endIndex": index + len(header[c]),
                            },
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    })
            if bolds:
                await _batch(creds, document_id, bolds)
            tables_written += 1

        return {
            "status": "ok",
            "document_id": document_id,
            "blocks": len(blocks),
            "tables": tables_written,
            "layout": await _apply_layout(creds, document_id),
        }
    except UnconfirmedWrite as e:
        logger.warning("Unconfirmed write in docs_write_markdown: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"docs_write_markdown failed for {document_id}: {e}")
        return {"error": str(e), "document_id": document_id}


# ISO A4 in points. New documents otherwise come out US Letter for this account.
_A4 = {
    "width": {"magnitude": 595.276, "unit": "PT"},
    "height": {"magnitude": 841.89, "unit": "PT"},
}


def _layout_requests(document: dict) -> List[Dict[str, Any]]:
    """Page layout for a finished document, worked out from its real structure.

    Each item comes from reading the Ex zones report's PDF page by page
    (2026-09-13):
    - insertTable always leaves an empty paragraph in front of the table, so a
      heading's keepWithNext held on to that blank line and "3.1 Usporedna
      tablica" ended page 3 with its table on page 4;
    - tables broke mid-row and carried on overleaf with no header row;
    - the page was US Letter.
    """
    content = (document.get("body") or {}).get("content") or []
    requests: List[Dict[str, Any]] = [{
        "updateDocumentStyle": {"documentStyle": {"pageSize": _A4}, "fields": "pageSize"}
    }]
    glued = set()

    def paragraph_text(el: dict) -> str:
        elements = (el.get("paragraph") or {}).get("elements") or []
        return "".join(e.get("textRun", {}).get("content", "") for e in elements)

    def is_blank(el: dict) -> bool:
        para = el.get("paragraph")
        if para is None:
            return False
        elements = para.get("elements") or []
        return not paragraph_text(el).strip() and not any("pageBreak" in e for e in elements)

    def glue(el: dict) -> None:
        start, end = el.get("startIndex"), el.get("endIndex")
        if start is None or end is None or start in glued:
            return
        glued.add(start)
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"keepWithNext": True},
                "fields": "keepWithNext",
            }
        })

    for i, el in enumerate(content):
        if "table" in el:
            j = i - 1
            while j >= 0 and is_blank(content[j]):
                glue(content[j])
                j -= 1
            # "...pri formalnoj upotrebi:" introduces the table; in the rebuilt
            # report it ended page 8 with its table overleaf.
            if j >= 0 and paragraph_text(content[j]).rstrip().endswith(":"):
                glue(content[j])
            if el.get("startIndex") is None:
                continue
            where = {"index": el["startIndex"]}
            requests.append({
                "updateTableRowStyle": {
                    "tableStartLocation": where,
                    "tableRowStyle": {"preventOverflow": True},
                    "fields": "preventOverflow",
                }
            })
            rows = el["table"].get("rows") or len(el["table"].get("tableRows") or [])
            if rows > 1:
                requests.append({
                    "pinTableHeaderRows": {
                        "tableStartLocation": where,
                        "pinnedHeaderRowsCount": 1,
                    }
                })
            continue
        style = (el.get("paragraph") or {}).get("paragraphStyle") or {}
        if style.get("namedStyleType", "").startswith("HEADING"):
            j = i + 1
            while j < len(content) and is_blank(content[j]):
                glue(content[j])
                j += 1
    return requests


async def _apply_layout(creds, document_id: str) -> str:
    """Run the layout pass. The words are already in, so a miss is only noted."""
    from tools.api_implementations.docs_api import (
        docs_batch_update as _batch,
        docs_get_document as _get,
    )
    try:
        await _batch(creds, document_id, _layout_requests(await _get(creds, document_id)))
        return "ok"
    except Exception as e:
        logger.warning("Layout pass failed for %s: %s", document_id, e)
        return f"skipped: {e}"


def _shift_requests(requests: List[Dict[str, Any]], offset: int) -> List[Dict[str, Any]]:
    """Move every index in a request batch by `offset`."""
    def shift(obj):
        if isinstance(obj, dict):
            return {
                k: (v + offset if k in ("index", "startIndex", "endIndex")
                    and isinstance(v, int) else shift(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [shift(v) for v in obj]
        return obj
    return [shift(r) for r in requests]


def _last_table_cells(document: dict) -> List[tuple]:
    """(row, column, text-insert index) for every cell of the LAST table.

    The last one, because tables are appended in document order and this runs
    straight after inserting one.
    """
    content = (document.get("body") or {}).get("content") or []
    tables = [el for el in content if "table" in el]
    if not tables:
        return []
    cells = []
    for r, row in enumerate(tables[-1]["table"].get("tableRows", [])):
        for c, cell in enumerate(row.get("tableCells", [])):
            inner = cell.get("content") or []
            if inner:
                cells.append((r, c, inner[0].get("startIndex", 0)))
    return cells


def get_docs_adk_tools(credentials=None) -> List:
    """
    Get all Docs ADK tools as plain Python functions.

    ADK automatically wraps these functions as tools based on:
    - Function signature (type hints)
    - Docstring (description and parameter docs)

    Args:
        credentials: Not used - included for API compatibility. Tools use OAuth from token.

    Returns:
        List of docs tool functions
    """
    tools = [
        docs_create_document,
        docs_get_document,
        docs_insert_text,
        docs_batch_update,
        docs_format_text,
        format_markdown_for_docs,
        docs_write_markdown,
    ]

    logger.info(f"Docs ADK tools loaded: {len(tools)} tools")
    return tools


if __name__ == "__main__":
    # Test tool loading
    tools = get_docs_adk_tools()
    print(f"[OK] Docs ADK tools loaded: {len(tools)} tools")

    for tool in tools:
        print(f"   - {tool.__name__}")

    print("\n[CAPABILITIES] Google Docs Operations:")
    print("   - Create new documents")
    print("   - Read document content")
    print("   - Insert and format text")
    print("   - Batch updates for complex formatting")
    print("   - Markdown-to-Docs conversion")
    print("   - Professional document structure")
