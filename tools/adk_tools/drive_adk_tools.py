"""
Google Drive ADK Tools

ADK-compatible wrapper for Google Drive operations.
Converts existing Drive API implementations to plain Python async functions
that ADK can auto-wrap as tools.

Tools:
- drive_search_files: Search for files using Drive Query Language
- drive_get_file: Get file metadata and content
- drive_upload_file: Upload new files
- drive_update_file: Update existing files
- drive_delete_file: Move files to trash
- drive_share_file: Share files with users or publicly
- drive_create_folder: Create new folders
- drive_move_file: Move files between folders
- translate_drive_query: Convert natural language to Drive Query Language

Key Feature: Natural language search with automatic query translation!

Usage:
    from tools.adk_tools.drive_adk_tools import get_drive_adk_tools

    # Get all Drive tools
    drive_tools = get_drive_adk_tools()

    # Create agent with Drive tools
    agent = LlmAgent(
        name="librarian",
        model="gemini-3.5-flash",
        tools=drive_tools
    )
"""

from typing import Optional
from google.oauth2.credentials import Credentials
import logging

from tools.resilience.retry_handler import UnconfirmedWrite
logger = logging.getLogger(__name__)


# ============================================================================
# CREDENTIAL HELPER
# ============================================================================

def _get_credentials() -> Optional[Credentials]:
    """
    Get OAuth2 credentials from token file.

    Returns:
        Credentials object or None if not authenticated
    """
    try:
        from auth.credential_store import get_credential_store

        credential_store = get_credential_store()
        creds = credential_store.get_credentials()

        if creds is None:
            logger.warning("No credentials found. Use: python tools/oauth_cli.py --auth")
            return None

        return creds

    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


# ============================================================================
# ADK TOOL FUNCTIONS
# ============================================================================

async def drive_search_files(
    query: str,
    max_results: int = 10,
    order_by: Optional[str] = None
) -> dict:
    """
    Search for files in Google Drive using Drive Query Language or natural language.

    This tool supports both:
    1. Drive Query Language (QPL) - precise queries like: name contains 'budget' and trashed = false
    2. Natural language - conversational queries like: find budget from last week

    For natural language queries, use translate_drive_query first to convert
    to proper Drive Query Language, then use this tool with the translated query.

    Args:
        query: Search query. Can be Drive Query Language or natural language.
            Examples:
            - "name contains 'report' and mimeType = 'application/pdf'"
            - "'me' in owners and modifiedTime > '2024-01-01T00:00:00'"
            - "trashed = false and starred = true"
        max_results: Maximum number of files to return (default: 10)
        order_by: Sort order. Examples:
            - "modifiedTime desc" - newest first
            - "modifiedTime" - oldest first
            - "name" - alphabetical by name
            - "folder,name" - folders first, then by name

    Returns:
        Dictionary with search results:
        {
            "files": [
                {
                    "id": "abc123...",
                    "name": "Budget_2024.xlsx",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "modifiedTime": "2024-01-15T10:30:00Z",
                    "webViewLink": "https://docs.google.com/...",
                    "owners": [{"displayName": "John Doe"}]
                }
            ],
            "count": 1,
            "query": "name contains 'budget'"
        }

    Example:
        >>> # Search for PDFs modified this week
        >>> results = await drive_search_files(
        ...     "mimeType = 'application/pdf' and modifiedTime > '2024-12-01T00:00:00'",
        ...     max_results=5
        ... )
        >>> for file in results['files']:
        ...     print(f"{file['name']} - {file['webViewLink']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_search_files as drive_search_impl
        result = await drive_search_impl(creds, query, max_results, order_by)
        return result

    except Exception as e:
        logger.error(f"Error searching Drive files: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_get_file(
    file_id: str,
    include_content: bool = False
) -> dict:
    """
    Get metadata and optionally content of a specific file by ID.

    Retrieves detailed information about a file including:
    - File name, type, size
    - Created and modified timestamps
    - Owners and permissions
    - Web view link
    - Optionally: file content (for supported file types)

    Args:
        file_id: Google Drive file ID (from URL or search results).
            Example: "1abc123def456..."
        include_content: Whether to download and include file content.
            Default: False (metadata only).
            Set to True to download file contents.
            Note: Large files may be slow to download.

    Returns:
        Dictionary with file information:
        {
            "metadata": {
                "id": "abc123...",
                "name": "Document.pdf",
                "mimeType": "application/pdf",
                "size": "1024000",
                "createdTime": "2024-01-01T00:00:00Z",
                "modifiedTime": "2024-01-15T10:30:00Z",
                "webViewLink": "https://drive.google.com/file/d/...",
                "owners": [...],
                "permissions": [...],
                "parents": ["folder_id..."],
                "trashed": false
            },
            "content": "..." (if include_content=True),
            "encoding": "utf-8" or "base64" (if include_content=True)
        }

    Example:
        >>> # Get file metadata only
        >>> file_info = await drive_get_file("1abc123...")
        >>> print(f"File: {file_info['metadata']['name']}")
        >>>
        >>> # Get file with content
        >>> file_with_content = await drive_get_file("1abc123...", include_content=True)
        >>> content = file_with_content['content']
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_get_file as drive_get_impl
        result = await drive_get_impl(creds, file_id, include_content)
        return result

    except Exception as e:
        logger.error(f"Error getting Drive file: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_upload_file(
    file_name: str,
    content: str,
    mime_type: str,
    parent_folder_id: Optional[str] = None
) -> dict:
    """
    Upload a new file to Google Drive.

    Creates a new file in the user's Drive with the specified name, content, and type.
    Optionally place it in a specific folder.

    Args:
        file_name: Name for the uploaded file (e.g., "Report.pdf", "Data.csv")
        content: File content as string.
            - For text files: plain text string
            - For binary files: base64-encoded string
        mime_type: MIME type of the file.
            Common types:
            - "text/plain" - Text files
            - "application/pdf" - PDFs
            - "image/jpeg", "image/png" - Images
            - "application/vnd.google-apps.document" - Google Docs
            - "application/vnd.google-apps.spreadsheet" - Google Sheets
        parent_folder_id: Optional folder ID where file should be placed.
            If not provided, file goes to root ("My Drive").

    Returns:
        Dictionary with upload result:
        {
            "file_id": "abc123...",
            "name": "Report.pdf",
            "webViewLink": "https://drive.google.com/file/d/...",
            "mimeType": "application/pdf",
            "status": "uploaded"
        }

    Example:
        >>> # Upload a text file
        >>> result = await drive_upload_file(
        ...     "Notes.txt",
        ...     "Meeting notes from 2024-12-04",
        ...     "text/plain"
        ... )
        >>> print(f"Uploaded: {result['webViewLink']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_upload_file as drive_upload_impl
        result = await drive_upload_impl(creds, file_name, content, mime_type, parent_folder_id)
        return result

    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in drive_upload_file: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Error uploading file to Drive: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_upload_local_file(
    file_path: str,
    drive_file_name: Optional[str] = None,
    parent_folder_id: Optional[str] = None
) -> dict:
    """
    Upload a local file from disk to Google Drive.

    Reads a file from the local filesystem and uploads it to Drive.
    Automatically detects MIME type from the file extension.
    Use this when you have a file path (e.g., from PDF generation).

    Args:
        file_path: Absolute path to the local file to upload.
            Example: "output/invoices/invoice_1_1_1_abc123.pdf"
        drive_file_name: Optional name for the file in Drive.
            If not provided, uses the original filename.
        parent_folder_id: Optional folder ID in Drive.
            If not provided, file goes to root ("My Drive").

    Returns:
        Dictionary with upload result:
        {
            "file_id": "abc123...",
            "name": "invoice.pdf",
            "webViewLink": "https://drive.google.com/file/d/...",
            "mimeType": "application/pdf",
            "status": "uploaded"
        }

    Example:
        >>> result = await drive_upload_local_file(
        ...     file_path="output/invoices/invoice_1_1_1_abc123.pdf",
        ...     drive_file_name="Račun 1-1-1.pdf"
        ... )
        >>> print(f"Uploaded: {result['webViewLink']}")
    """
    import os
    import base64
    import mimetypes

    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        # Resolve relative paths from project root
        if not os.path.isabs(file_path):
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            file_path = os.path.join(project_root, file_path)

        if not os.path.exists(file_path):
            return {
                "error": f"File not found: {file_path}",
                "status": "failed"
            }

        # Determine file name and MIME type
        actual_name = drive_file_name or os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "application/octet-stream"

        # Read file and base64 encode
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        content_b64 = base64.b64encode(file_bytes).decode('utf-8')

        logger.info(f"Uploading local file to Drive: {actual_name} ({len(file_bytes)} bytes, {mime_type})")

        # Use existing upload implementation
        from tools.api_implementations.drive_api import drive_upload_file as drive_upload_impl
        result = await drive_upload_impl(creds, actual_name, content_b64, mime_type, parent_folder_id)
        return result

    except Exception as e:
        logger.error(f"Error uploading local file to Drive: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_update_file(
    file_id: str,
    content: Optional[str] = None,
    name: Optional[str] = None
) -> dict:
    """
    Update an existing file's content or metadata.

    Can update file content, rename the file, or both.
    At least one of content or name must be provided.

    Args:
        file_id: Google Drive file ID to update
        content: New file content (optional).
            - For text files: plain text string
            - For binary files: base64-encoded string
            If not provided, content remains unchanged.
        name: New file name (optional).
            If not provided, name remains unchanged.

    Returns:
        Dictionary with update result:
        {
            "file_id": "abc123...",
            "name": "New_Name.pdf",
            "modifiedTime": "2024-12-04T14:30:00Z",
            "status": "updated"
        }

    Example:
        >>> # Rename a file
        >>> result = await drive_update_file(
        ...     "1abc123...",
        ...     name="Budget_Final_2024.xlsx"
        ... )
        >>>
        >>> # Update file content
        >>> result = await drive_update_file(
        ...     "1abc123...",
        ...     content="Updated meeting notes"
        ... )
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_update_file as drive_update_impl
        result = await drive_update_impl(creds, file_id, content, name)
        return result

    except Exception as e:
        logger.error(f"Error updating Drive file: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_delete_file(
    file_id: str
) -> dict:
    """
    Move a file to trash (soft delete).

    Does not permanently delete the file - it goes to trash and can be recovered.
    User can permanently delete from trash later if needed.

    Args:
        file_id: Google Drive file ID to trash

    Returns:
        Dictionary with delete result:
        {
            "file_id": "abc123...",
            "status": "trashed",
            "message": "File moved to trash (recoverable)"
        }

    Example:
        >>> result = await drive_delete_file("1abc123...")
        >>> print(result['message'])  # "File moved to trash (recoverable)"

    Note:
        Files in trash can be recovered by the user from the Drive web interface.
        They are automatically permanently deleted after 30 days.
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_delete_file as drive_delete_impl
        result = await drive_delete_impl(creds, file_id)
        return result

    except Exception as e:
        logger.error(f"Error deleting Drive file: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_share_file(
    file_id: str,
    email: Optional[str] = None,
    role: str = "reader",
    type: str = "user"
) -> dict:
    """
    Share a file with users or make it publicly accessible.

    Grants permissions to access a file. Can share with specific users,
    groups, domains, or make publicly accessible.

    Args:
        file_id: Google Drive file ID to share
        email: Email address to share with (omit for public sharing).
            Examples:
            - "john@example.com" - specific user
            - None - for public sharing (set type="anyone")
        role: Permission level (default: "reader").
            Options:
            - "reader" - Can view only
            - "writer" - Can edit
            - "commenter" - Can comment
        type: Permission type (default: "user").
            Options:
            - "user" - Specific user by email
            - "group" - Google Group
            - "domain" - Anyone in organization domain
            - "anyone" - Public access (anyone with link)

    Returns:
        Dictionary with sharing result:
        {
            "file_id": "abc123...",
            "permission_id": "perm123...",
            "role": "reader",
            "type": "user",
            "email": "john@example.com",
            "status": "shared"
        }

    Example:
        >>> # Share with specific user as reader
        >>> result = await drive_share_file(
        ...     "1abc123...",
        ...     email="john@example.com",
        ...     role="reader"
        ... )
        >>>
        >>> # Share publicly (anyone with link can view)
        >>> result = await drive_share_file(
        ...     "1abc123...",
        ...     role="reader",
        ...     type="anyone"
        ... )

    Warning:
        Be careful with type="anyone" - this makes the file publicly accessible!
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_share_file as drive_share_impl
        result = await drive_share_impl(creds, file_id, email, role, type)
        return result

    except Exception as e:
        logger.error(f"Error sharing Drive file: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_create_folder(
    folder_name: str,
    parent_folder_id: Optional[str] = None
) -> dict:
    """
    Create a new folder in Google Drive.

    Creates a folder to organize files. Optionally create it inside
    another folder, or in root ("My Drive") by default.

    Args:
        folder_name: Name for the new folder (e.g., "Invoices 2024", "Archive")
        parent_folder_id: Optional parent folder ID where new folder should be created.
            If not provided, folder is created in root ("My Drive").

    Returns:
        Dictionary with folder creation result:
        {
            "folder_id": "abc123...",
            "name": "Invoices 2024",
            "webViewLink": "https://drive.google.com/drive/folders/...",
            "status": "created"
        }

    Example:
        >>> # Create folder in root
        >>> result = await drive_create_folder("Archive 2024")
        >>> folder_id = result['folder_id']
        >>>
        >>> # Create subfolder inside existing folder
        >>> result = await drive_create_folder(
        ...     "Q1",
        ...     parent_folder_id=folder_id
        ... )
        >>> print(f"Created: {result['webViewLink']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_create_folder as drive_create_impl
        result = await drive_create_impl(creds, folder_name, parent_folder_id)
        return result

    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in drive_create_folder: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Error creating Drive folder: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_move_file(
    file_id: str,
    new_parent_id: str
) -> dict:
    """
    Move a file to a different folder.

    Changes the parent folder of a file, effectively moving it to
    a new location in the Drive folder structure.

    Args:
        file_id: Google Drive file ID to move
        new_parent_id: ID of the destination folder

    Returns:
        Dictionary with move result:
        {
            "file_id": "abc123...",
            "name": "Document.pdf",
            "new_parent_id": "folder123...",
            "status": "moved"
        }

    Example:
        >>> # First find or create destination folder
        >>> folder_result = await drive_create_folder("Archive")
        >>> folder_id = folder_result['folder_id']
        >>>
        >>> # Then move file to that folder
        >>> result = await drive_move_file("1abc123...", folder_id)
        >>> print(f"Moved {result['name']} to Archive folder")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.drive_api import drive_move_file as drive_move_impl
        result = await drive_move_impl(creds, file_id, new_parent_id)
        return result

    except Exception as e:
        logger.error(f"Error moving Drive file: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def drive_convert_to_sheets(
    file_id: str
) -> dict:
    """
    Convert an uploaded file (Excel .xlsx/.xls, CSV, ODS) to native Google Sheets format.

    Use this when the Sheets API returns "This operation is not supported for this document",
    which means the file is not a native Google Sheet. This tool creates a Google Sheets
    copy that can be analyzed with sheets tools.

    Args:
        file_id: Google Drive file ID of the uploaded file to convert

    Returns:
        Dictionary with the new Google Sheets file info:
        {
            "original_file_id": "abc123...",
            "new_file_id": "xyz789...",
            "new_file_name": "Sales Q4 2025 (Google Sheets)",
            "new_file_url": "https://docs.google.com/spreadsheets/d/xyz789.../edit",
            "status": "converted"
        }

    Example:
        >>> # Librarian found .xlsx file with ID "1abc..."
        >>> # Analyst failed with "not supported for this document"
        >>> result = await drive_convert_to_sheets("1abc...")
        >>> new_id = result['new_file_id']
        >>> # Now use new_id with analyst's sheets tools
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from googleapiclient.discovery import build
        from tools.google_api_client import aexecute

        service = build('drive', 'v3', credentials=creds)

        # Get original file name
        original = await aexecute(service.files().get(fileId=file_id, fields='name'))
        original_name = original.get('name', 'Untitled')

        # Copy file with conversion to Google Sheets
        copy_body = {
            'name': f"{original_name} (Google Sheets)",
            'mimeType': 'application/vnd.google-apps.spreadsheet'
        }
        copied = await aexecute(service.files().copy(fileId=file_id, body=copy_body))

        new_id = copied['id']
        new_name = copied.get('name', copy_body['name'])

        logger.info(f"Converted '{original_name}' to Google Sheets: {new_id}")

        return {
            "original_file_id": file_id,
            "new_file_id": new_id,
            "new_file_name": new_name,
            "new_file_url": f"https://docs.google.com/spreadsheets/d/{new_id}/edit",
            "status": "converted"
        }

    except Exception as e:
        logger.error(f"Error converting to Google Sheets: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


async def translate_drive_query(
    natural_query: str
) -> dict:
    """
    Translate natural language to Google Drive Query Language (QPL).

    THIS IS A KEY TOOL FOR USER-FRIENDLY SEARCHES!

    Converts conversational search queries into precise Drive Query Language
    that can be used with drive_search_files. This allows users to search
    using natural language instead of learning complex query syntax.

    Args:
        natural_query: Natural language search query.
            Examples:
            - "find budget from last week"
            - "my presentations"
            - "shared with john@example.com"
            - "starred documents"
            - "spreadsheets modified today"
            - "pdfs in trash"

    Returns:
        Dictionary with translated query:
        {
            "original_query": "find budget from last week",
            "translated_query": "name contains 'budget' and modifiedTime > '2024-11-27T00:00:00' and trashed = false",
            "status": "translated"
        }

    Supported Patterns:
        - **By name**: "find budget" → name contains 'budget'
        - **By type**: "spreadsheets" → mimeType = 'application/vnd.google-apps.spreadsheet'
        - **By time**: "last week" → modifiedTime > '2024-XX-XX'
        - **By owner**: "my files" → 'me' in owners
        - **By sharing**: "shared with john@example.com" → 'john@example.com' in readers
        - **By starred**: "starred" → starred = true
        - **By trashed**: "in trash" → trashed = true

    Example Workflow:
        >>> # Step 1: Translate natural language to QPL
        >>> translation = await translate_drive_query("find budget spreadsheets from last month")
        >>> qpl_query = translation['translated_query']
        >>> # → "name contains 'budget' and mimeType = 'application/vnd.google-apps.spreadsheet' and modifiedTime > '2024-11-04T00:00:00' and trashed = false"
        >>>
        >>> # Step 2: Use translated query to search
        >>> results = await drive_search_files(qpl_query)
        >>> for file in results['files']:
        ...     print(file['name'])

    Note:
        ALWAYS use this tool first when user provides a natural language search query.
        Then use the translated query with drive_search_files.
    """
    try:
        from tools.custom_tools.drive_query_translator import translate_drive_query as translate_impl

        translated_query = translate_impl(natural_query)

        logger.info(f"Translated query: '{natural_query}' → '{translated_query}'")

        return {
            "original_query": natural_query,
            "translated_query": translated_query,
            "status": "translated"
        }

    except Exception as e:
        logger.error(f"Error translating Drive query: {e}")
        return {
            "error": str(e),
            "status": "failed"
        }


# ============================================================================
# TOOL FACTORY
# ============================================================================

def get_drive_adk_tools(credentials: Optional[Credentials] = None) -> list:
    """
    Get all Google Drive ADK tools as a list.

    Returns plain Python async functions that ADK will automatically
    wrap as tools based on type hints and docstrings.

    Args:
        credentials: Optional OAuth2 credentials. If None, tools will
            fetch credentials from token file.

    Returns:
        List of 10 Drive tool functions

    Tools included:
        1. drive_search_files - Search for files
        2. drive_get_file - Get file metadata/content
        3. drive_upload_file - Upload new files (base64/text content)
        4. drive_upload_local_file - Upload local files from disk path
        5. drive_update_file - Update files
        6. drive_delete_file - Trash files
        7. drive_share_file - Share files
        8. drive_create_folder - Create folders
        9. drive_move_file - Move files
        10. drive_convert_to_sheets - Convert .xlsx/CSV to Google Sheets
        11. translate_drive_query - Natural language → QPL (KEY FEATURE!)

    Example:
        >>> from tools.adk_tools.drive_adk_tools import get_drive_adk_tools
        >>> from google.adk.agents import LlmAgent
        >>>
        >>> drive_tools = get_drive_adk_tools()
        >>> agent = LlmAgent(
        ...     name="librarian",
        ...     model="gemini-3.5-flash",
        ...     tools=drive_tools
        ... )
    """
    tools = [
        drive_search_files,
        drive_get_file,
        drive_upload_file,
        drive_upload_local_file,
        drive_update_file,
        drive_delete_file,
        drive_share_file,
        drive_create_folder,
        drive_move_file,
        drive_convert_to_sheets,  # Convert .xlsx/CSV to Google Sheets
        translate_drive_query,  # KEY: Natural language search!
    ]

    logger.info(f"Created {len(tools)} Google Drive ADK tools")
    return tools


# ============================================================================
# TOOL METADATA
# ============================================================================

def get_drive_capabilities() -> dict:
    """
    Get capabilities summary for Drive tools.

    Returns:
        Dictionary describing tool capabilities
    """
    return {
        "service": "Google Drive",
        "tool_count": 9,
        "capabilities": [
            "Search files with Drive Query Language",
            "Natural language search translation",
            "Get file metadata and content",
            "Upload new files",
            "Update file content and metadata",
            "Delete files (move to trash)",
            "Share files with permissions",
            "Create and organize folders",
            "Move files between folders"
        ],
        "special_features": [
            "translate_drive_query: Convert natural language to QPL",
            "Smart query translation (time ranges, file types, owners)",
            "Support for Google Workspace file types",
            "Permission management (reader/writer/commenter)",
            "Public and private sharing options"
        ],
        "best_practices": [
            "ALWAYS use translate_drive_query for natural language searches",
            "Search before uploading to avoid duplicates",
            "Confirm before sharing publicly (type='anyone')",
            "Remember delete is soft (moves to trash, recoverable)",
            "Use folders for organization"
        ]
    }


if __name__ == "__main__":
    # Test tool creation
    tools = get_drive_adk_tools()
    print(f"[OK] Created {len(tools)} Drive ADK tools")

    for tool in tools:
        print(f"  - {tool.__name__}: {tool.__doc__.split(chr(10))[0] if tool.__doc__ else 'No description'}")

    print("\nCapabilities:")
    caps = get_drive_capabilities()
    for cap in caps['capabilities']:
        print(f"  - {cap}")

    print("\nSpecial Features:")
    for feat in caps['special_features']:
        print(f"  - {feat}")
