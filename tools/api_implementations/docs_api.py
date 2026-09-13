"""
Google Docs API Implementation

Real Google Docs API functions using Google Docs API v1
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
import logging

from tools.resilience.retry_handler import (
    with_retry, RetryConfig, report_unconfirmed,
)
from tools.resilience.circuit_breaker import with_circuit_breaker
from tools.resilience.rate_limiter import with_rate_limit
from tools.resilience.cache import with_cache, invalidates_cache
from tools.google_api_client import aexecute

logger = logging.getLogger(__name__)


# ============================================================================
# GOOGLE DOCS API FUNCTIONS
# ============================================================================

@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new document on every call.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje dokumenta")
@invalidates_cache("docs")
async def docs_create_document(
    credentials: Credentials,
    title: str,
    content: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new Google Docs document

    Args:
        credentials: OAuth2 credentials
        title: Document title
        content: Initial content (optional)

    Returns:
        Dictionary with created document details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Creating Google Doc: {title}")

        # Create document
        doc = await aexecute(service.documents().create(body={'title': title}))
        doc_id = doc['documentId']

        logger.info(f"Document created: {doc_id}")

        # Add initial content if provided
        if content:
            logger.info("Adding initial content to document")
            requests = [{
                'insertText': {
                    'location': {'index': 1},
                    'text': content
                }
            }]

            await aexecute(service.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': requests}
            ))

        return {
            'document_id': doc_id,
            'title': doc['title'],
            'revision_id': doc.get('revisionId'),
            'document_url': f"https://docs.google.com/document/d/{doc_id}/edit",
            'status': 'created'
        }

    except HttpError as e:
        logger.error(f"Failed to create Google Doc: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_create_document: {e}")
        raise


@with_circuit_breaker("docs")
@with_cache("docs", ttl=300, user_id_param="credentials")  # Cache for 5 min
@with_rate_limit("docs", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def docs_get_document(
    credentials: Credentials,
    document_id: str,
    include_suggestions: bool = False
) -> Dict[str, Any]:
    """
    Get content and metadata of a Google Docs document

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        include_suggestions: Include suggestions mode changes (default: False)

    Returns:
        Dictionary with document content and metadata

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Getting Google Doc: {document_id}")

        # Get document
        params = {'documentId': document_id}
        if include_suggestions:
            params['suggestionsViewMode'] = 'SUGGESTIONS_INLINE'

        doc = await aexecute(service.documents().get(**params))

        # Extract text content
        text_content = _extract_text_from_document(doc)

        result = {
            'document_id': doc['documentId'],
            'title': doc['title'],
            'text_content': text_content,
            'revision_id': doc.get('revisionId'),
            'document_url': f"https://docs.google.com/document/d/{document_id}/edit",
            'body': doc.get('body', {}),
            'inline_objects': doc.get('inlineObjects', {}),
            'lists': doc.get('lists', {}),
            'named_styles': doc.get('namedStyles', {})
        }

        logger.info(f"Document retrieved: {doc['title']} ({len(text_content)} chars)")

        return result

    except HttpError as e:
        logger.error(f"Failed to get Google Doc: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_get_document: {e}")
        raise


@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# NOTE: no @with_retry here — inserting the same text twice writes it twice.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("umetanje teksta u dokument")
@invalidates_cache("docs")
async def docs_insert_text(
    credentials: Credentials,
    document_id: str,
    text: str,
    index: int = 1
) -> Dict[str, Any]:
    """
    Insert text at a specific position in the document

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        text: Text to insert
        index: Position to insert text (default: 1 = beginning)

    Returns:
        Dictionary with update status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Inserting text into Google Doc: {document_id} at index {index}")

        # Insert text
        requests = [{
            'insertText': {
                'location': {'index': index},
                'text': text
            }
        }]

        result = await aexecute(service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ))

        logger.info(f"Text inserted successfully: {len(text)} chars")

        return {
            'document_id': document_id,
            'revision_id': result.get('documentId'),
            'writes': result.get('writes', 0),
            'status': 'text_inserted'
        }

    except HttpError as e:
        logger.error(f"Failed to insert text: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_insert_text: {e}")
        raise


@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# NOTE: no @with_retry here — replace is idempotent only when the replacement does not contain the
# search text: A -> AA run twice gives AAAA.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("zamjena teksta u dokumentu")
@invalidates_cache("docs")
async def docs_replace_text(
    credentials: Credentials,
    document_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = False
) -> Dict[str, Any]:
    """
    Find and replace text in the document

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        find_text: Text to find
        replace_text: Text to replace with
        match_case: Case-sensitive search (default: False)

    Returns:
        Dictionary with replacement count and status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Replacing text in Google Doc: {document_id}")
        logger.info(f"Find: '{find_text}' -> Replace: '{replace_text}'")

        # Replace all occurrences
        requests = [{
            'replaceAllText': {
                'containsText': {
                    'text': find_text,
                    'matchCase': match_case
                },
                'replaceText': replace_text
            }
        }]

        result = await aexecute(service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ))

        # Count occurrences replaced
        occurrences_changed = result.get('replies', [{}])[0].get('replaceAllText', {}).get('occurrencesChanged', 0)

        logger.info(f"Text replaced: {occurrences_changed} occurrences")

        return {
            'document_id': document_id,
            'occurrences_changed': occurrences_changed,
            'find_text': find_text,
            'replace_text': replace_text,
            'status': 'replaced'
        }

    except HttpError as e:
        logger.error(f"Failed to replace text: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_replace_text: {e}")
        raise


@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# NOTE: no @with_retry here — appending the same text twice writes it twice.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("dodavanje teksta u dokument")
@invalidates_cache("docs")
async def docs_append_text(
    credentials: Credentials,
    document_id: str,
    text: str
) -> Dict[str, Any]:
    """
    Append text to the end of the document

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        text: Text to append

    Returns:
        Dictionary with update status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Appending text to Google Doc: {document_id}")

        # Get current document to find end index
        doc = await aexecute(service.documents().get(documentId=document_id))
        content = doc.get('body', {}).get('content', [])

        # Find the end index (last element's endIndex - 1)
        end_index = 1
        if content:
            end_index = content[-1].get('endIndex', 1) - 1

        # Append text
        requests = [{
            'insertText': {
                'location': {'index': end_index},
                'text': '\n' + text
            }
        }]

        result = await aexecute(service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ))

        logger.info(f"Text appended successfully: {len(text)} chars")

        return {
            'document_id': document_id,
            'text_length': len(text),
            'end_index': end_index,
            'status': 'text_appended'
        }

    except HttpError as e:
        logger.error(f"Failed to append text: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_append_text: {e}")
        raise


@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# NOTE: no @with_retry here — caller-supplied requests; insert/append among them are not idempotent.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("batch izmjena dokumenta")
@invalidates_cache("docs")
async def docs_batch_update(
    credentials: Credentials,
    document_id: str,
    requests: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Perform batch updates on a document (multiple operations at once)

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        requests: List of request objects (see Google Docs API documentation)

    Returns:
        Dictionary with batch update results

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Performing batch update on Google Doc: {document_id}")
        logger.info(f"Number of requests: {len(requests)}")

        # Execute batch update
        result = await aexecute(service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ))

        logger.info(f"Batch update completed: {document_id}")

        return {
            'document_id': result.get('documentId'),
            'revision_id': result.get('revisionId'),
            'replies': result.get('replies', []),
            'writes': len(result.get('writeControl', {}).get('requiredRevisionId', '')),
            'status': 'batch_updated'
        }

    except HttpError as e:
        logger.error(f"Failed to perform batch update: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_batch_update: {e}")
        raise


@with_circuit_breaker("docs")
@with_rate_limit("docs", user_id_param="credentials")
# Retry is safe: applying the same style to the same range twice looks
# identical.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("docs")
async def docs_format_text(
    credentials: Credentials,
    document_id: str,
    start_index: int,
    end_index: int,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    font_size: Optional[int] = None
) -> Dict[str, Any]:
    """
    Apply text formatting (bold, italic, underline, font size) to a text range

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        start_index: Start position (1-based index)
        end_index: End position (1-based index)
        bold: Apply bold formatting (optional)
        italic: Apply italic formatting (optional)
        underline: Apply underline formatting (optional)
        font_size: Font size in points (optional)

    Returns:
        Dictionary with formatting results

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.docs_service()

        logger.info(f"Formatting text in document {document_id} from {start_index} to {end_index}")

        # Build text style update
        text_style = {}
        fields = []

        if bold is not None:
            text_style['bold'] = bold
            fields.append('bold')

        if italic is not None:
            text_style['italic'] = italic
            fields.append('italic')

        if underline is not None:
            text_style['underline'] = underline
            fields.append('underline')

        if font_size is not None:
            text_style['fontSize'] = {
                'magnitude': font_size,
                'unit': 'PT'
            }
            fields.append('fontSize')

        if not text_style:
            logger.warning("No formatting options specified")
            return {
                'document_id': document_id,
                'status': 'no_changes',
                'message': 'No formatting options provided'
            }

        # Create batch update request
        requests = [{
            'updateTextStyle': {
                'range': {
                    'startIndex': start_index,
                    'endIndex': end_index
                },
                'textStyle': text_style,
                'fields': ','.join(fields)
            }
        }]

        # Execute batch update
        result = await aexecute(service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ))

        logger.info(f"Text formatting applied successfully")

        return {
            'document_id': result.get('documentId'),
            'revision_id': result.get('revisionId'),
            'start_index': start_index,
            'end_index': end_index,
            'applied_formatting': text_style,
            'status': 'formatted'
        }

    except HttpError as e:
        logger.error(f"Failed to format text: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_format_text: {e}")
        raise


@with_circuit_breaker("docs")
@with_cache("docs", ttl=300, user_id_param="credentials")  # Cache for 5 min
@with_rate_limit("docs", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def docs_export_document(
    credentials: Credentials,
    document_id: str,
    mime_type: str = "application/pdf"
) -> Dict[str, Any]:
    """
    Export a Google Docs document to different formats

    Args:
        credentials: OAuth2 credentials
        document_id: Google Docs document ID
        mime_type: Export format MIME type
            - "application/pdf" (PDF)
            - "application/vnd.openxmlformats-officedocument.wordprocessingml.document" (DOCX)
            - "text/plain" (TXT)
            - "text/html" (HTML)

    Returns:
        Dictionary with exported content (base64 encoded for binary formats)

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        import base64

        api_client = GoogleAPIClient(credentials=credentials)

        # Use Drive API for export
        drive_service = api_client.drive_service()

        logger.info(f"Exporting Google Doc: {document_id} as {mime_type}")

        # Export document
        request = drive_service.files().export(
            fileId=document_id,
            mimeType=mime_type
        )

        content = await aexecute(request)

        # Encode binary content
        if isinstance(content, bytes):
            content_encoded = base64.b64encode(content).decode('utf-8')
            is_binary = True
        else:
            content_encoded = content
            is_binary = False

        logger.info(f"Document exported successfully: {len(content)} bytes")

        return {
            'document_id': document_id,
            'mime_type': mime_type,
            'content': content_encoded,
            'content_size': len(content),
            'is_binary': is_binary,
            'status': 'exported'
        }

    except HttpError as e:
        logger.error(f"Failed to export document: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in docs_export_document: {e}")
        raise


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _extract_text_from_document(doc: Dict[str, Any]) -> str:
    """
    Extract plain text content from document structure

    Args:
        doc: Document object from Docs API

    Returns:
        Plain text content
    """
    text_parts = []

    content = doc.get('body', {}).get('content', [])

    for element in content:
        if 'paragraph' in element:
            paragraph = element['paragraph']
            for text_element in paragraph.get('elements', []):
                if 'textRun' in text_element:
                    text_content = text_element['textRun'].get('content', '')
                    text_parts.append(text_content)

    return ''.join(text_parts)


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_docs_tools(tool_registry):
    """
    Register all Google Docs tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # docs_create_document
    tool_registry.register_tool(
        name="docs_create_document",
        function=docs_create_document,
        description="Create a new Google Docs document with optional initial content.",
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title"
                },
                "content": {
                    "type": "string",
                    "description": "Initial content (optional)"
                }
            },
            "required": ["title"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_get_document
    tool_registry.register_tool(
        name="docs_get_document",
        function=docs_get_document,
        description="Get content and metadata of a Google Docs document by ID.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "include_suggestions": {
                    "type": "boolean",
                    "description": "Include suggestions mode changes (default: False)",
                    "default": False
                }
            },
            "required": ["document_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_insert_text
    tool_registry.register_tool(
        name="docs_insert_text",
        function=docs_insert_text,
        description="Insert text at a specific position in the document.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "text": {
                    "type": "string",
                    "description": "Text to insert"
                },
                "index": {
                    "type": "integer",
                    "description": "Position to insert text (default: 1 = beginning)",
                    "default": 1
                }
            },
            "required": ["document_id", "text"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_replace_text
    tool_registry.register_tool(
        name="docs_replace_text",
        function=docs_replace_text,
        description="Find and replace text in a Google Docs document.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "find_text": {
                    "type": "string",
                    "description": "Text to find"
                },
                "replace_text": {
                    "type": "string",
                    "description": "Text to replace with"
                },
                "match_case": {
                    "type": "boolean",
                    "description": "Case-sensitive search (default: False)",
                    "default": False
                }
            },
            "required": ["document_id", "find_text", "replace_text"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_append_text
    tool_registry.register_tool(
        name="docs_append_text",
        function=docs_append_text,
        description="Append text to the end of a Google Docs document.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "text": {
                    "type": "string",
                    "description": "Text to append"
                }
            },
            "required": ["document_id", "text"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_batch_update
    tool_registry.register_tool(
        name="docs_batch_update",
        function=docs_batch_update,
        description="Perform batch updates on a document (multiple operations at once).",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "requests": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of request objects (see Google Docs API documentation)"
                }
            },
            "required": ["document_id", "requests"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_format_text
    tool_registry.register_tool(
        name="docs_format_text",
        function=docs_format_text,
        description="Apply text formatting (bold, italic, underline, font size) to a specific text range in a document.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "start_index": {
                    "type": "integer",
                    "description": "Start position (1-based index)"
                },
                "end_index": {
                    "type": "integer",
                    "description": "End position (1-based index)"
                },
                "bold": {
                    "type": "boolean",
                    "description": "Apply bold formatting (optional)"
                },
                "italic": {
                    "type": "boolean",
                    "description": "Apply italic formatting (optional)"
                },
                "underline": {
                    "type": "boolean",
                    "description": "Apply underline formatting (optional)"
                },
                "font_size": {
                    "type": "integer",
                    "description": "Font size in points (optional)"
                }
            },
            "required": ["document_id", "start_index", "end_index"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # docs_export_document
    tool_registry.register_tool(
        name="docs_export_document",
        function=docs_export_document,
        description="Export a Google Docs document to PDF, DOCX, TXT, or HTML format.",
        parameters={
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Google Docs document ID"
                },
                "mime_type": {
                    "type": "string",
                    "description": "Export format: 'application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' (DOCX), 'text/plain', 'text/html'",
                    "default": "application/pdf"
                }
            },
            "required": ["document_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # Custom Tools: Docs Formatter
    from tools.custom_tools.docs_formatter import format_markdown_for_docs

    tool_registry.register_tool(
        name="format_markdown_for_docs",
        function=format_markdown_for_docs,
        description="Convert Markdown text to Google Docs API batch update requests. Use this tool to format documents from Markdown.",
        parameters={
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": "Markdown text to convert to Docs format"
                }
            },
            "required": ["markdown"]
        },
        requires_auth=False
    )

    logger.info("Google Docs tools registered successfully")
