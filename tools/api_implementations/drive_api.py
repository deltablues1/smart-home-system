"""
Google Drive API Implementation

Real Drive API functions using Google Drive API v3
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
import io
import re
import logging

from tools.resilience.retry_handler import (
    with_retry, RetryConfig, report_unconfirmed,
)
from tools.resilience.circuit_breaker import with_circuit_breaker
from tools.resilience.rate_limiter import with_rate_limit
from tools.resilience.cache import with_cache, invalidates_cache
from tools.google_api_client import aexecute

logger = logging.getLogger(__name__)


# Croatian/English filler words that creep into a "find the X file" request and
# pollute a `name contains '...'` Drive query (e.g. "customers tablicu").
_DRIVE_NOISE_WORDS = {
    "tablicu", "tablica", "tablice", "tablicom",
    "datoteku", "datoteka", "datoteke", "dokument", "dokumenta",
    "file", "fajl", "fajla", "spreadsheet", "sheet", "excel",
    "find", "search", "nadji", "nađi", "pronadji", "pronađi", "otvori",
    "the", "mi", "na", "u", "od",
}


def _relax_name_query(query: str) -> Optional[str]:
    """Build a looser fallback for a name query that found nothing.

    Handles both `name = '<phrase>'` (exact match — the most common reason a
    lookup wrongly returns zero, since the real file is "Sales Q4 2025 (1).xlsx"
    not exactly "sales q4 2025") and `name contains '<phrase>'`. Drops filler
    words and ORs the distinctive tokens as a `contains` search.

    Returns the relaxed Drive query, or None when there is nothing to relax.
    """
    m = re.search(r"name\s*(=|contains)\s*'([^']+)'", query, re.IGNORECASE)
    if not m:
        return None
    op = m.group(1)
    phrase = m.group(2).strip()
    tokens = [t for t in re.split(r"\s+", phrase) if len(t) >= 3 and "'" not in t]
    if not tokens:
        return None
    # Exact `=` is brittle (case + whole-name), so always relax it. A multi-word
    # `contains` that missed gets split into tokens. A single-token `contains` is
    # already optimal, so leave it alone.
    if op == "=" or len(tokens) > 1:
        significant = [t for t in tokens if t.lower() not in _DRIVE_NOISE_WORDS] or tokens
        clauses = " or ".join(f"name contains '{t}'" for t in significant)
        return f"({clauses}) and trashed = false"
    return None


# ============================================================================
# DRIVE API FUNCTIONS
# ============================================================================

@with_circuit_breaker("drive")
@with_cache("drive", ttl=600, user_id_param="credentials")  # Cache for 10 min (file metadata changes less frequently)
@with_rate_limit("drive", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def drive_search_files(
    credentials: Credentials,
    query: str,
    max_results: int = 10,
    order_by: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for files in Google Drive

    Args:
        credentials: OAuth2 credentials
        query: Search query (Drive Query Language or natural language)
        max_results: Maximum number of files to return (use 1000 for all results)
        order_by: Sort order (e.g., 'modifiedTime desc', 'name')

    Returns:
        Dictionary with list of files

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Searching Drive files: query='{query}', max={max_results}")

        # Collect all files across pages
        all_files = []
        page_token = None
        page_num = 0

        # Drive API max pageSize is 1000
        page_size = min(max_results, 1000)

        while True:
            page_num += 1

            # Build request with pagination
            request_params = {
                'q': query,
                'pageSize': page_size,
                'fields': 'nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink, owners)'
            }

            if order_by:
                request_params['orderBy'] = order_by

            if page_token:
                request_params['pageToken'] = page_token

            # Execute search
            results = await aexecute(service.files().list(**request_params))
            files = results.get('files', [])
            all_files.extend(files)

            logger.info(f"Page {page_num}: Found {len(files)} files (total: {len(all_files)})")

            # Check if we have more pages
            page_token = results.get('nextPageToken')

            # Stop if no more pages or reached max_results
            if not page_token or len(all_files) >= max_results:
                break

        # Fallback: a name-contains search that found nothing usually means the
        # query carried filler words (e.g. "customers tablicu"). Retry with the
        # distinctive tokens only so the real file still matches.
        if not all_files:
            relaxed = _relax_name_query(query)
            if relaxed and relaxed != query:
                logger.info(f"Drive search empty — retrying relaxed query: {relaxed!r}")
                fb = await aexecute(service.files().list(
                    q=relaxed,
                    pageSize=page_size,
                    fields='files(id, name, mimeType, modifiedTime, size, webViewLink, owners)',
                ))
                all_files = fb.get('files', [])
                if all_files:
                    query = relaxed  # report the query that actually matched

        # Trim to max_results if needed
        if len(all_files) > max_results:
            all_files = all_files[:max_results]

        logger.info(f"Total found: {len(all_files)} files")

        return {
            'files': all_files,
            'count': len(all_files),
            'query': query
        }

    except HttpError as e:
        logger.error(f"Drive search failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_search_files: {e}")
        raise


@with_circuit_breaker("drive")
@with_cache("drive", ttl=600, user_id_param="credentials")  # Cache for 10 min (metadata changes less frequently)
@with_rate_limit("drive", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def drive_get_file(
    credentials: Credentials,
    file_id: str,
    include_content: bool = False
) -> Dict[str, Any]:
    """
    Get metadata and optionally content of a specific file

    Args:
        credentials: OAuth2 credentials
        file_id: Google Drive file ID
        include_content: Whether to download file content

    Returns:
        Dictionary with file metadata and optionally content

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Getting Drive file: {file_id}, include_content={include_content}")

        # Get file metadata
        file_metadata = await aexecute(service.files().get(
            fileId=file_id,
            fields='id, name, mimeType, size, createdTime, modifiedTime, webViewLink, owners, permissions, parents, trashed'
        ))

        result = {
            'metadata': file_metadata
        }

        # Download content if requested
        if include_content:
            logger.info(f"Downloading file content: {file_id}")

            # Check if file is a Google Workspace doc (needs export)
            mime_type = file_metadata.get('mimeType', '')

            if mime_type.startswith('application/vnd.google-apps'):
                # Export Google Workspace files
                export_mime = _get_export_mime_type(mime_type)
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                # Download regular files
                request = service.files().get_media(fileId=file_id)

            # Download to bytes
            file_content = io.BytesIO()
            downloader = await aexecute(request)

            # Handle content based on type
            if isinstance(downloader, bytes):
                # Check if it looks like text or binary based on mime_type
                is_text = mime_type.startswith('text/') or mime_type in [
                    'application/json', 'application/xml', 'application/javascript', 
                    'application/x-yaml'
                ]
                
                if is_text:
                    try:
                        result['content'] = downloader.decode('utf-8')
                        result['encoding'] = 'utf-8'
                    except UnicodeDecodeError:
                        # Fallback to base64 if decoding fails
                        import base64
                        result['content'] = base64.b64encode(downloader).decode('utf-8')
                        result['encoding'] = 'base64'
                else:
                    # Binary content (images, pdfs, etc.) -> Base64
                    import base64
                    result['content'] = base64.b64encode(downloader).decode('utf-8')
                    result['encoding'] = 'base64'
            else:
                result['content'] = str(downloader)
                result['encoding'] = 'str'
            
            result['content_size'] = len(downloader) if isinstance(downloader, bytes) else 0

        logger.info(f"File retrieved successfully: {file_metadata.get('name')}")

        return result

    except HttpError as e:
        logger.error(f"Failed to get Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_get_file: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# NOTE: no @with_retry here — upload creates a new file on every call; a retry leaves two copies.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("upload datoteke na Drive")
@invalidates_cache("drive")
async def drive_upload_file(
    credentials: Credentials,
    file_name: str,
    content: str,
    mime_type: str,
    parent_folder_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Upload a new file to Google Drive

    Args:
        credentials: OAuth2 credentials
        file_name: Name of the file
        content: File content (text or base64 encoded)
        mime_type: MIME type of the file
        parent_folder_id: Optional parent folder ID

    Returns:
        Dictionary with uploaded file details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Uploading file to Drive: {file_name}")

        # Prepare file metadata
        file_metadata = {
            'name': file_name,
            'mimeType': mime_type
        }

        if parent_folder_id:
            file_metadata['parents'] = [parent_folder_id]

        # Determine if content is base64 (for binary files)
        # Binary MIME types should be base64 encoded
        binary_mime_types = [
            'application/pdf',
            'application/zip',
            'application/octet-stream',
            'image/',
            'video/',
            'audio/'
        ]

        is_binary = any(mime_type.startswith(prefix) for prefix in binary_mime_types)

        # Decode base64 for binary files, encode UTF-8 for text files
        if is_binary:
            import base64
            try:
                # Assume content is base64 encoded for binary files
                file_bytes = base64.b64decode(content)
                logger.info(f"Decoded base64 content: {len(file_bytes)} bytes")
            except Exception as e:
                logger.warning(f"Failed to decode base64, treating as raw bytes: {e}")
                file_bytes = content.encode('utf-8')
        else:
            # Text files - encode as UTF-8
            file_bytes = content.encode('utf-8')

        # Create media upload
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes),
            mimetype=mime_type,
            resumable=True
        )

        # Upload file
        uploaded_file = await aexecute(service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name, mimeType, webViewLink'
        ))

        logger.info(f"File uploaded successfully: {uploaded_file['id']}")

        return {
            'id': uploaded_file['id'],
            'name': uploaded_file['name'],
            'mime_type': uploaded_file['mimeType'],
            'web_view_link': uploaded_file.get('webViewLink'),
            'status': 'uploaded'
        }

    except HttpError as e:
        logger.error(f"Failed to upload Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_upload_file: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: sets a known file id to a known state.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("drive")
async def drive_update_file(
    credentials: Credentials,
    file_id: str,
    content: Optional[str] = None,
    name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing file's content or metadata

    Args:
        credentials: OAuth2 credentials
        file_id: Google Drive file ID
        content: New file content (optional)
        name: New file name (optional)

    Returns:
        Dictionary with updated file details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Updating Drive file: {file_id}")

        # Prepare update
        file_metadata = {}
        if name:
            file_metadata['name'] = name

        media = None
        if content:
            # Get current mime type
            current_file = await aexecute(service.files().get(fileId=file_id, fields='mimeType'))
            mime_type = current_file.get('mimeType', 'text/plain')

            media = MediaIoBaseUpload(
                io.BytesIO(content.encode('utf-8')),
                mimetype=mime_type,
                resumable=True
            )

        # Update file
        updated_file = await aexecute(service.files().update(
            fileId=file_id,
            body=file_metadata if file_metadata else None,
            media_body=media,
            fields='id, name, mimeType, modifiedTime'
        ))

        logger.info(f"File updated successfully: {file_id}")

        return {
            'id': updated_file['id'],
            'name': updated_file['name'],
            'mime_type': updated_file['mimeType'],
            'modified_time': updated_file.get('modifiedTime'),
            'status': 'updated'
        }

    except HttpError as e:
        logger.error(f"Failed to update Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_update_file: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: a second delete returns 404, which is not retried, and
# the end state is the same either way.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("drive")
async def drive_delete_file(
    credentials: Credentials,
    file_id: str
) -> Dict[str, Any]:
    """
    Move a file to trash (soft delete)

    Args:
        credentials: OAuth2 credentials
        file_id: Google Drive file ID

    Returns:
        Dictionary with deletion status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Moving Drive file to trash: {file_id}")

        # files().delete() would be permanent; trash keeps it recoverable ~30 days
        await aexecute(service.files().update(fileId=file_id, body={'trashed': True}))

        logger.info(f"File moved to trash: {file_id}")

        return {
            'id': file_id,
            'status': 'trashed'
        }

    except HttpError as e:
        logger.error(f"Failed to delete Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_delete_file: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: reading who has access changes nothing.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def drive_list_permissions(
    credentials: Credentials,
    file_id: str,
) -> Dict[str, Any]:
    """Who can already open this file.

    Needed before granting access on someone's behalf: a permission that was
    already there is not ours to take away if the thing we granted it for
    then fails.

    Every page, not the first one. A partial list would say "this person has
    no access" about somebody who does, and the caller uses that answer to
    decide what it may revoke later.
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        permissions = []
        page_token = None
        while True:
            result = await aexecute(service.permissions().list(
                fileId=file_id,
                fields='nextPageToken, permissions(id, type, role, emailAddress)',
                pageSize=100,
                pageToken=page_token,
            ))
            permissions.extend(result.get('permissions', []))
            page_token = result.get('nextPageToken')
            if not page_token:
                break

        return {'file_id': file_id, 'permissions': permissions}

    except HttpError as e:
        logger.error(f"Failed to list permissions for {file_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_list_permissions: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: a second delete of the same permission returns 404, which is
# not retried, and the end state is the same either way.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("drive")
async def drive_revoke_permission(
    credentials: Credentials,
    file_id: str,
    permission_id: str,
) -> Dict[str, Any]:
    """Take back one specific permission, by the id that created it."""
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        await aexecute(service.permissions().delete(
            fileId=file_id, permissionId=permission_id,
        ))
        logger.info(f"Revoked permission {permission_id} on {file_id}")
        return {'file_id': file_id, 'permission_id': permission_id, 'status': 'revoked'}

    except HttpError as e:
        logger.error(f"Failed to revoke {permission_id} on {file_id}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_revoke_permission: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: granting the same role to the same address again leaves
# the same access.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("drive")
async def drive_share_file(
    credentials: Credentials,
    file_id: str,
    email: Optional[str] = None,
    role: str = "reader",
    type: str = "user",
    send_notification: bool = True,
) -> Dict[str, Any]:
    """
    Share a file with users or make it publicly accessible

    Args:
        credentials: OAuth2 credentials
        file_id: Google Drive file ID
        email: Email address to share with (omit for public sharing)
        role: Permission role ('reader', 'writer', 'commenter')
        type: Permission type ('user', 'group', 'domain', 'anyone')
        send_notification: For user/group shares, whether Drive sends its own
            notification email to the recipient (default True). Set False when
            the app already emails the recipient, to avoid a duplicate message.

    Returns:
        Dictionary with sharing details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Sharing Drive file: {file_id} with {email or 'public'}")

        # Prepare permission
        permission = {
            'type': type,
            'role': role
        }

        if email and type in ['user', 'group']:
            permission['emailAddress'] = email

        # Create permission. For user/group shares, control whether Google Drive
        # sends its own "X shared a document with you" notification email — when
        # the app already emails the recipient (e.g. mailer), suppress it to avoid
        # a duplicate message. sendNotificationEmail is not applicable to "anyone".
        create_kwargs = dict(
            fileId=file_id,
            body=permission,
            fields='id, type, role, emailAddress',
        )
        if type in ['user', 'group']:
            create_kwargs['sendNotificationEmail'] = send_notification
        created_permission = await aexecute(service.permissions().create(**create_kwargs))

        logger.info(f"File shared successfully: {file_id}")

        return {
            'file_id': file_id,
            'permission_id': created_permission['id'],
            'type': created_permission['type'],
            'role': created_permission['role'],
            'email': created_permission.get('emailAddress'),
            'status': 'shared'
        }

    except HttpError as e:
        logger.error(f"Failed to share Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_share_file: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new folder every time, same name or not.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje mape na Driveu")
@invalidates_cache("drive")
async def drive_create_folder(
    credentials: Credentials,
    folder_name: str,
    parent_folder_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new folder in Google Drive

    Args:
        credentials: OAuth2 credentials
        folder_name: Name of the folder
        parent_folder_id: Optional parent folder ID

    Returns:
        Dictionary with created folder details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Creating Drive folder: {folder_name}")

        # Prepare folder metadata
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }

        if parent_folder_id:
            folder_metadata['parents'] = [parent_folder_id]

        # Create folder
        folder = await aexecute(service.files().create(
            body=folder_metadata,
            fields='id, name, webViewLink'
        ))

        logger.info(f"Folder created successfully: {folder['id']}")

        return {
            'id': folder['id'],
            'name': folder['name'],
            'web_view_link': folder.get('webViewLink'),
            'status': 'created'
        }

    except HttpError as e:
        logger.error(f"Failed to create Drive folder: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_create_folder: {e}")
        raise


@with_circuit_breaker("drive")
@with_rate_limit("drive", user_id_param="credentials")
# Retry is safe: the file ends up under the same parent either way.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("drive")
async def drive_move_file(
    credentials: Credentials,
    file_id: str,
    new_parent_id: str
) -> Dict[str, Any]:
    """
    Move a file to a different folder

    Args:
        credentials: OAuth2 credentials
        file_id: Google Drive file ID
        new_parent_id: ID of the destination folder

    Returns:
        Dictionary with move status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.drive_service()

        logger.info(f"Moving Drive file: {file_id} to {new_parent_id}")

        # Get current parents
        file = await aexecute(service.files().get(fileId=file_id, fields='parents'))
        previous_parents = ",".join(file.get('parents', []))

        # Move file
        moved_file = await aexecute(service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=previous_parents,
            fields='id, parents'
        ))

        logger.info(f"File moved successfully: {file_id}")

        return {
            'id': moved_file['id'],
            'parents': moved_file.get('parents', []),
            'status': 'moved'
        }

    except HttpError as e:
        logger.error(f"Failed to move Drive file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in drive_move_file: {e}")
        raise


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _get_export_mime_type(google_mime_type: str) -> str:
    """
    Get export MIME type for Google Workspace documents

    Args:
        google_mime_type: Google Workspace MIME type

    Returns:
        Export MIME type
    """
    export_map = {
        'application/vnd.google-apps.document': 'text/plain',
        'application/vnd.google-apps.spreadsheet': 'text/csv',
        'application/vnd.google-apps.presentation': 'text/plain',
    }
    return export_map.get(google_mime_type, 'text/plain')


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_drive_tools(tool_registry):
    """
    Register all Drive tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # drive_search_files
    tool_registry.register_tool(
        name="drive_search_files",
        function=drive_search_files,
        description="Search for files in Google Drive using Drive Query Language (DQL) or natural language.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Maximum number of files (default: 10)", "default": 10},
                "order_by": {"type": "string", "description": "Sort order (e.g., 'modifiedTime desc')"}
            },
            "required": ["query"]
        }
    )

    # drive_get_file
    tool_registry.register_tool(
        name="drive_get_file",
        function=drive_get_file,
        description="Get metadata and content of a specific file by ID.",
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Google Drive file ID"},
                "include_content": {"type": "boolean", "description": "Download file content", "default": False}
            },
            "required": ["file_id"]
        }
    )

    # drive_upload_file
    tool_registry.register_tool(
        name="drive_upload_file",
        function=drive_upload_file,
        description="Upload a new file to Google Drive.",
        parameters={
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "File name"},
                "content": {"type": "string", "description": "File content"},
                "mime_type": {"type": "string", "description": "MIME type"},
                "parent_folder_id": {"type": "string", "description": "Parent folder ID (optional)"}
            },
            "required": ["file_name", "content", "mime_type"]
        }
    )

    # Other tools...
    tool_registry.register_tool(name="drive_update_file", function=drive_update_file, description="Update file", parameters={"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]})
    tool_registry.register_tool(name="drive_delete_file", function=drive_delete_file, description="Delete file", parameters={"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]})
    # drive_share_file
    tool_registry.register_tool(
        name="drive_share_file",
        function=drive_share_file,
        description="Share a Google Drive file with users or make it publicly accessible. Use type='anyone' for public sharing.",
        parameters={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID to share"
                },
                "email": {
                    "type": "string",
                    "description": "Email address to share with (omit for public sharing)"
                },
                "role": {
                    "type": "string",
                    "description": "Permission role: 'reader' (view only), 'writer' (can edit), 'commenter' (can comment)",
                    "enum": ["reader", "writer", "commenter"],
                    "default": "reader"
                },
                "type": {
                    "type": "string",
                    "description": "Permission type: 'user' (specific user), 'group', 'domain', 'anyone' (public)",
                    "enum": ["user", "group", "domain", "anyone"],
                    "default": "user"
                }
            },
            "required": ["file_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )
    tool_registry.register_tool(name="drive_create_folder", function=drive_create_folder, description="Create folder", parameters={"type": "object", "properties": {"folder_name": {"type": "string"}}, "required": ["folder_name"]})
    tool_registry.register_tool(name="drive_move_file", function=drive_move_file, description="Move file", parameters={"type": "object", "properties": {"file_id": {"type": "string"}, "new_parent_id": {"type": "string"}}, "required": ["file_id", "new_parent_id"]})

    logger.info("Drive tools registered successfully")
