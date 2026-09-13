"""
Gmail API Implementation

Real Gmail API functions using Google Gmail API v1
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import logging

from tools.resilience.retry_handler import (
    with_retry, RetryConfig, report_unconfirmed,
)
from tools.resilience.circuit_breaker import with_circuit_breaker
from tools.resilience.rate_limiter import with_rate_limit
from tools.resilience.cache import with_cache, invalidates_cache
from tools.google_api_client import aexecute

logger = logging.getLogger(__name__)


def _detect_markdown(text: str) -> bool:
    """Return True if text appears to contain Markdown formatting."""
    import re
    patterns = [
        r'^#{1,6}\s',          # headings
        r'\*\*.+?\*\*',        # bold
        r'^\s*[-*]\s',         # bullet lists
        r'^\d+\.\s',           # numbered lists
        r'\[.+?\]\(.+?\)',     # links
        r'^\s*\|.+\|',        # tables
    ]
    return any(re.search(p, text, re.MULTILINE) for p in patterns)


def _markdown_to_html(text: str) -> str:
    """Convert Markdown text to HTML. Falls back to plain text wrapped in <pre> if markdown not available."""
    try:
        import markdown as md_lib
        html_body = md_lib.markdown(text, extensions=['tables', 'fenced_code'])
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
  h1, h2, h3 {{ color: #1a1a1a; margin-top: 1.4em; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-family: monospace; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f0f0f0; }}
  a {{ color: #1a73e8; }}
</style></head><body>{html_body}</body></html>"""
    except ImportError:
        import html
        return f"<pre>{html.escape(text)}</pre>"


def _plain_from_markdown(text: str) -> str:
    """Strip basic markdown syntax for plain-text fallback."""
    import re
    clean = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    clean = re.sub(r'\*\*(.+?)\*\*', r'\1', clean)
    clean = re.sub(r'\*(.+?)\*', r'\1', clean)
    clean = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', clean)
    return clean


def validate_attachment_path(attachment_path: str) -> tuple:
    """Resolve and sandbox-check an attachment path.

    Returns (resolved_path, None) when the file is inside an allowed dir and
    exists; (attachment_path, error_message) otherwise. Shared by the send
    implementation AND the ADK wrapper — the wrapper must validate BEFORE
    auto-sharing linked docs, or a doomed send could still leak doc access.
    """
    import os

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    resolved = attachment_path
    if not os.path.isabs(resolved):
        resolved = os.path.join(project_root, resolved)

    # Sandbox: attachments may only come from designated output dirs —
    # otherwise the LLM could be tricked into mailing .env, tokens, certs...
    allowed_dirs = [
        os.path.realpath(os.path.join(project_root, d))
        for d in os.getenv("GMAIL_ATTACHMENT_DIRS", "output,uploads,temp").split(",")
        if d.strip()
    ]
    real_path = os.path.realpath(resolved)
    if not any(
        real_path == d or real_path.startswith(d + os.sep)
        for d in allowed_dirs
    ):
        return attachment_path, (
            f"Attachment path not allowed: {attachment_path}. Only files "
            "under the project's output/uploads/temp directories can be "
            "attached. Email NOT sent."
        )
    # isfile, not exists: a directory would pass exists() and explode at
    # open() — AFTER the linked docs were already shared.
    if not os.path.isfile(resolved):
        return attachment_path, (
            f"Attachment not found or not a file: {attachment_path}. Email NOT sent."
        )
    max_bytes = int(os.getenv("GMAIL_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)))
    size = os.path.getsize(resolved)
    if size > max_bytes:
        return attachment_path, (
            f"Attachment too large ({size} B, max {max_bytes} B): "
            f"{attachment_path}. Email NOT sent."
        )
    return resolved, None


# ============================================================================
# GMAIL API FUNCTIONS
# ============================================================================

@with_circuit_breaker("gmail")
@with_cache("gmail", ttl=60, user_id_param="credentials")  # Cache for 1 min (emails change frequently)
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_search_threads(
    credentials: Credentials,
    query: str,
    max_results: int = 10
) -> Dict[str, Any]:
    """
    Search Gmail threads using Gmail search query syntax

    Args:
        credentials: OAuth2 credentials
        query: Gmail search query (e.g., 'from:john@example.com', 'subject:meeting', 'is:unread')
        max_results: Maximum number of threads to return (default: 10)

    Returns:
        Dictionary with:
        - threads: List of thread objects with id, snippet
        - result_size_estimate: Total number of matching threads

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        # Create API client
        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        # Search threads
        logger.info(f"Searching Gmail threads: query='{query}', max_results={max_results}")

        results = await aexecute(service.users().threads().list(
            userId='me',
            q=query,
            maxResults=max_results
        ))

        threads = results.get('threads', [])
        result_size = results.get('resultSizeEstimate', 0)

        # Get thread details for each thread
        thread_details = []
        for thread in threads:
            thread_id = thread['id']
            thread_data = await aexecute(service.users().threads().get(
                userId='me',
                id=thread_id,
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date']
            ))

            # Extract snippet and basic info
            messages = thread_data.get('messages', [])
            if messages:
                # Use last message for From/Date (shows latest sender, not original)
                latest_msg = messages[-1]
                first_msg = messages[0]
                latest_headers = {h['name']: h['value'] for h in latest_msg.get('payload', {}).get('headers', [])}
                first_headers = {h['name']: h['value'] for h in first_msg.get('payload', {}).get('headers', [])}

                thread_details.append({
                    'id': thread_id,
                    'snippet': thread_data.get('snippet', ''),
                    'from': latest_headers.get('From', ''),
                    'to': latest_headers.get('To', ''),
                    'subject': first_headers.get('Subject', ''),
                    'date': latest_headers.get('Date', ''),
                    'message_count': len(messages)
                })

        logger.info(f"Found {len(thread_details)} threads")

        return {
            'threads': thread_details,
            'result_size_estimate': result_size,
            'query': query
        }

    except HttpError as e:
        logger.error(f"Gmail search failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_search_threads: {e}")
        raise


@with_circuit_breaker("gmail")
@with_cache("gmail", ttl=60, user_id_param="credentials")  # Cache for 1 min
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_get_thread(
    credentials: Credentials,
    thread_id: str
) -> Dict[str, Any]:
    """
    Get full content of a Gmail thread by ID

    Args:
        credentials: OAuth2 credentials
        thread_id: Gmail thread ID

    Returns:
        Dictionary with thread details including all messages

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        logger.info(f"Getting Gmail thread: {thread_id}")

        # Get full thread
        thread = await aexecute(service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full'
        ))

        # Parse messages
        messages = []
        for msg in thread.get('messages', []):
            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

            # Extract body
            body = _extract_message_body(msg.get('payload', {}))

            messages.append({
                'id': msg['id'],
                'message_id': headers.get('Message-ID') or headers.get('Message-Id', ''),
                'from': headers.get('From', ''),
                'to': headers.get('To', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
                'body': body,
                'snippet': msg.get('snippet', '')
            })

        logger.info(f"Retrieved thread with {len(messages)} messages")

        return {
            'id': thread_id,
            'messages': messages,
            'message_count': len(messages)
        }

    except HttpError as e:
        logger.error(f"Failed to get Gmail thread: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_get_thread: {e}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", cost=100, user_id_param="credentials")
# NOTE: no @with_retry here — send is not idempotent. A timeout after Gmail
# accepted the message would resend it and the recipient would get duplicates.
@invalidates_cache("gmail")
async def gmail_send_message(
    credentials: Credentials,
    to: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachment_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Send a Gmail message with optional file attachment.

    Args:
        credentials: OAuth2 credentials
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text or HTML)
        thread_id: Optional thread ID to reply to
        cc: Optional CC email addresses (comma-separated)
        bcc: Optional BCC email addresses (comma-separated)
        attachment_path: Optional local file path to attach (e.g., PDF invoice)

    Returns:
        Dictionary with sent message details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        logger.info(f"Sending Gmail message: to={to}, subject='{subject}'")

        # If replying to a thread, fetch the last message's Message-ID for proper threading
        original_message_id = None
        references = None
        if thread_id:
            try:
                thread_data = await aexecute(service.users().threads().get(
                    userId='me',
                    id=thread_id,
                    format='metadata',
                    metadataHeaders=['Message-ID', 'References', 'Subject']
                ))
                thread_messages = thread_data.get('messages', [])
                if thread_messages:
                    last_msg = thread_messages[-1]
                    headers = {h['name']: h['value'] for h in last_msg.get('payload', {}).get('headers', [])}
                    original_message_id = headers.get('Message-ID') or headers.get('Message-Id')
                    references = headers.get('References', '')
                    # Use original subject with Re: prefix if not already present
                    original_subject = headers.get('Subject', '')
                    if original_subject and not subject.lower().startswith('re:'):
                        subject = f"Re: {original_subject}"
                    logger.info(f"Reply threading: In-Reply-To={original_message_id}")
            except Exception as e:
                logger.warning(f"Could not fetch thread headers for reply threading: {e}")

        # Create message
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject

        # Set threading headers for proper reply display
        if original_message_id:
            message['In-Reply-To'] = original_message_id
            if references:
                message['References'] = f"{references} {original_message_id}"
            else:
                message['References'] = original_message_id

        if cc:
            message['cc'] = cc
        if bcc:
            message['bcc'] = bcc

        # Add body — convert Markdown to HTML if detected
        if _detect_markdown(body):
            html_content = _markdown_to_html(body)
            plain_content = _plain_from_markdown(body)
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(plain_content, 'plain', 'utf-8'))
            alt.attach(MIMEText(html_content, 'html', 'utf-8'))
            message.attach(alt)
        else:
            message.attach(MIMEText(body, 'plain', 'utf-8'))

        # Add attachment if provided
        if attachment_path:
            import os
            attachment_path, path_error = validate_attachment_path(attachment_path)
            if path_error:
                logger.error(f"Attachment rejected: {path_error}")
                return {"error": path_error, "status": "failed"}

            filename = os.path.basename(attachment_path)
            ext = os.path.splitext(filename)[1].lower()
            subtype_map = {'.pdf': 'pdf', '.xlsx': 'vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                           '.docx': 'vnd.openxmlformats-officedocument.wordprocessingml.document',
                           '.png': 'png', '.jpg': 'jpeg', '.jpeg': 'jpeg'}
            subtype = subtype_map.get(ext, 'octet-stream')

            with open(attachment_path, 'rb') as f:
                file_attachment = MIMEApplication(f.read(), _subtype=subtype)
            file_attachment.add_header('Content-Disposition', 'attachment', filename=filename)
            message.attach(file_attachment)
            logger.info(f"Attached file: {filename} ({os.path.getsize(attachment_path)} bytes)")

        # Encode message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        # Prepare send request
        send_request = {'raw': raw_message}

        # If replying to a thread, add threadId
        if thread_id:
            send_request['threadId'] = thread_id
            logger.info(f"Replying to thread: {thread_id}")

        # Send message
        sent_message = await aexecute(service.users().messages().send(
            userId='me',
            body=send_request
        ))

        logger.info(f"Message sent successfully: id={sent_message['id']}")

        return {
            'id': sent_message['id'],
            'thread_id': sent_message.get('threadId'),
            'label_ids': sent_message.get('labelIds', []),
            'status': 'sent'
        }

    except HttpError as e:
        logger.error(f"Failed to send Gmail message: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_send_message: {e}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", cost=50, user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new draft every time; a retry leaves duplicates in the
# drafts folder.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje nacrta maila")
@invalidates_cache("gmail")
async def gmail_create_draft(
    credentials: Credentials,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a Gmail draft message without sending

    Args:
        credentials: OAuth2 credentials
        to: Recipient email address
        subject: Email subject
        body: Email body (plain text or HTML)
        cc: Optional CC email addresses (comma-separated)

    Returns:
        Dictionary with draft details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        logger.info(f"Creating Gmail draft: to={to}, subject='{subject}'")

        # Create message
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject

        if cc:
            message['cc'] = cc

        # Add body — convert Markdown to HTML if detected
        if _detect_markdown(body):
            html_content = _markdown_to_html(body)
            plain_content = _plain_from_markdown(body)
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(plain_content, 'plain', 'utf-8'))
            alt.attach(MIMEText(html_content, 'html', 'utf-8'))
            message.attach(alt)
        else:
            message.attach(MIMEText(body, 'plain', 'utf-8'))

        # Encode message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        # Create draft
        draft = await aexecute(service.users().drafts().create(
            userId='me',
            body={'message': {'raw': raw_message}}
        ))

        logger.info(f"Draft created successfully: id={draft['id']}")

        return {
            'id': draft['id'],
            'message_id': draft['message']['id'],
            'status': 'draft'
        }

    except HttpError as e:
        logger.error(f"Failed to create Gmail draft: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_create_draft: {e}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("gmail")
async def gmail_modify_thread(
    credentials: Credentials,
    thread_id: str,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Modify labels on a Gmail thread

    Args:
        credentials: OAuth2 credentials
        thread_id: Gmail thread ID
        add_labels: Labels to add (e.g., ['STARRED', 'IMPORTANT'])
        remove_labels: Labels to remove (e.g., ['UNREAD', 'INBOX'])

    Returns:
        Dictionary with modified thread details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        logger.info(f"Modifying Gmail thread: {thread_id}")

        # Prepare modification request
        modify_request = {}
        if add_labels:
            modify_request['addLabelIds'] = add_labels
            logger.info(f"Adding labels: {add_labels}")
        if remove_labels:
            modify_request['removeLabelIds'] = remove_labels
            logger.info(f"Removing labels: {remove_labels}")

        # Modify thread
        modified_thread = await aexecute(service.users().threads().modify(
            userId='me',
            id=thread_id,
            body=modify_request
        ))

        logger.info(f"Thread modified successfully: {thread_id}")

        return {
            'id': modified_thread['id'],
            'label_ids': modified_thread.get('messages', [{}])[0].get('labelIds', []),
            'status': 'modified'
        }

    except HttpError as e:
        logger.error(f"Failed to modify Gmail thread: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_modify_thread: {e}")
        raise


@with_circuit_breaker("gmail")
@with_cache("gmail", ttl=900, user_id_param="credentials")  # Cache for 15 min (labels rarely change)
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_list_labels(
    credentials: Credentials
) -> Dict[str, Any]:
    """
    List all Gmail labels (system and user-created)

    Args:
        credentials: OAuth2 credentials

    Returns:
        Dictionary with list of labels

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        logger.info("Listing Gmail labels")

        # Get labels
        results = await aexecute(service.users().labels().list(userId='me'))
        labels = results.get('labels', [])

        # Organize labels by type
        system_labels = []
        user_labels = []

        for label in labels:
            label_data = {
                'id': label['id'],
                'name': label['name'],
                'type': label.get('type', 'user')
            }

            if label.get('type') == 'system':
                system_labels.append(label_data)
            else:
                user_labels.append(label_data)

        logger.info(f"Found {len(labels)} labels ({len(system_labels)} system, {len(user_labels)} user)")

        return {
            'labels': labels,
            'system_labels': system_labels,
            'user_labels': user_labels,
            'total_count': len(labels)
        }

    except HttpError as e:
        logger.error(f"Failed to list Gmail labels: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in gmail_list_labels: {e}")
        raise


# ============================================================================
# INBOUND / ATTACHMENT HELPERS  (Sprint Inbound A)
# ============================================================================

@with_circuit_breaker("gmail")
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_list_messages_with_attachments(
    credentials: Credentials,
    query: str = "has:attachment",
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    List Gmail messages that match *query* and have at least one attachment.

    Args:
        credentials:  OAuth2 credentials
        query:        Gmail search expression — default restricts to messages
                      with attachments.  Caller may add label filters, e.g.
                      ``has:attachment -label:IMPORTED``.
        max_results:  Maximum number of message stubs to return (1-500).

    Returns::

        {
            "messages": [
                {"id": "<message_id>", "thread_id": "<thread_id>"},
                ...
            ],
            "result_size_estimate": <int>,
            "query": "<effective query>",
        }
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        effective_query = query if "has:attachment" in query else f"has:attachment {query}".strip()
        logger.info(f"[Gmail/inbound] Listing messages: query={effective_query!r}, max={max_results}")

        result = service.users().messages().list(
            userId="me",
            q=effective_query,
            maxResults=min(max_results, 500),
        ).execute()

        messages = result.get("messages", [])
        logger.info(f"[Gmail/inbound] Found {len(messages)} message(s)")
        return {
            "messages": [{"id": m["id"], "thread_id": m.get("threadId", "")} for m in messages],
            "result_size_estimate": result.get("resultSizeEstimate", 0),
            "query": effective_query,
        }

    except HttpError as exc:
        logger.error(f"[Gmail/inbound] gmail_list_messages_with_attachments failed: {exc}")
        raise
    except Exception as exc:
        logger.error(f"[Gmail/inbound] Unexpected error: {exc}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_get_message_full(
    credentials: Credentials,
    message_id: str,
) -> Dict[str, Any]:
    """
    Fetch a single Gmail message in full format, returning headers and attachment
    metadata (but NOT attachment bytes — use gmail_download_attachment for those).

    Returns::

        {
            "id":          "<message_id>",
            "thread_id":   "<thread_id>",
            "from":        "<sender>",
            "subject":     "<subject>",
            "date":        "<RFC 2822 date string>",
            "received_at": "<ISO UTC timestamp>",   # derived from internalDate
            "snippet":     "<snippet>",
            "attachments": [
                {
                    "attachment_id": "<id>",
                    "filename":      "<filename>",
                    "mime_type":     "<mime_type>",
                    "size":          <int>,
                },
                ...
            ],
        }
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        from datetime import datetime, timezone

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full",
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        internal_date_ms = int(msg.get("internalDate", 0))
        received_at = (
            datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc).isoformat()
            if internal_date_ms else ""
        )

        attachments = _extract_attachment_metadata(msg.get("payload", {}))
        logger.info(
            f"[Gmail/inbound] Message {message_id}: {len(attachments)} attachment(s) found"
        )
        return {
            "id":          message_id,
            "thread_id":   msg.get("threadId", ""),
            "from":        headers.get("From", ""),
            "subject":     headers.get("Subject", ""),
            "date":        headers.get("Date", ""),
            "received_at": received_at,
            "snippet":     msg.get("snippet", ""),
            "attachments": attachments,
        }

    except HttpError as exc:
        logger.error(f"[Gmail/inbound] gmail_get_message_full({message_id}) failed: {exc}")
        raise
    except Exception as exc:
        logger.error(f"[Gmail/inbound] Unexpected error: {exc}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def gmail_download_attachment(
    credentials: Credentials,
    message_id: str,
    attachment_id: str,
) -> bytes:
    """
    Download attachment bytes from a Gmail message.

    Args:
        credentials:   OAuth2 credentials
        message_id:    Gmail message ID (from gmail_get_message_full)
        attachment_id: Attachment ID (from gmail_get_message_full ``attachments`` list)

    Returns:
        Raw attachment bytes.

    Raises:
        HttpError: on API error
        ValueError: if attachment data is missing
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        result = service.users().messages().attachments().get(
            userId="me",
            messageId=message_id,
            id=attachment_id,
        ).execute()

        data = result.get("data")
        if not data:
            raise ValueError(f"No data in attachment {attachment_id} of message {message_id}")

        raw = base64.urlsafe_b64decode(data)
        logger.info(
            f"[Gmail/inbound] Downloaded attachment {attachment_id} "
            f"({len(raw)} bytes) from message {message_id}"
        )
        return raw

    except HttpError as exc:
        logger.error(f"[Gmail/inbound] gmail_download_attachment({message_id}, {attachment_id}) failed: {exc}")
        raise
    except Exception as exc:
        logger.error(f"[Gmail/inbound] Unexpected error: {exc}")
        raise


# ============================================================================
# LABEL HELPERS  (Sprint Inbound A.1)
# ============================================================================

@invalidates_cache("gmail")
async def gmail_get_or_create_label(
    credentials: Credentials,
    label_name: str,
) -> str:
    """
    Resolve a user-defined label name to its Gmail label ID, creating it if absent.

    Gmail messages.modify() requires label **IDs**, not label names.
    System labels (INBOX, UNREAD, STARRED …) have IDs equal to their name and
    do not need resolution.  User labels must be looked up (or created) via the
    labels API.

    Args:
        credentials:  OAuth2 credentials
        label_name:   Display name of the label (e.g. "IMPORTED")

    Returns:
        The Gmail label ID string (e.g. "Label_12345678901234567").

    Note: result is NOT cached because labels are rarely created and we want
    exact-match semantics.  For hot-path use, resolve labels once per poll cycle
    and pass the resolved IDs directly to gmail_modify_message().
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        result = service.users().labels().list(userId="me").execute()
        for label in result.get("labels", []):
            if label.get("name") == label_name:
                return label["id"]

        # Not found — create it
        created = service.users().labels().create(
            userId="me",
            body={
                "name":                  label_name,
                "labelListVisibility":   "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        logger.info(f"[Gmail/label] Created label {label_name!r} → {created['id']}")
        return created["id"]

    except HttpError as exc:
        logger.error(f"[Gmail/label] gmail_get_or_create_label({label_name!r}) failed: {exc}")
        raise
    except Exception as exc:
        logger.error(f"[Gmail/label] Unexpected error: {exc}")
        raise


@with_circuit_breaker("gmail")
@with_rate_limit("gmail", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("gmail")
async def gmail_modify_message(
    credentials: Credentials,
    message_id: str,
    add_label_ids: Optional[List[str]] = None,
    remove_label_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Add or remove label IDs on a single Gmail **message** (not thread).

    Idempotency is message-scoped: labeling one message in a thread does not
    affect other messages in the same thread.  This is the correct granularity
    for e-račun intake — a supplier may send multiple invoices in the same
    email conversation and each one must be independently tracked.

    Args:
        credentials:      OAuth2 credentials
        message_id:       Gmail message ID (NOT thread ID)
        add_label_ids:    Label IDs to add (resolved via gmail_get_or_create_label)
        remove_label_ids: Label IDs to remove

    Returns:
        {"id": "<message_id>", "label_ids": [...], "status": "modified"}
    """
    try:
        from tools.google_api_client import GoogleAPIClient
        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.gmail_service()

        body: Dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"]    = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids

        modified = service.users().messages().modify(
            userId="me",
            id=message_id,
            body=body,
        ).execute()

        logger.info(
            f"[Gmail/label] Modified message {message_id}: "
            f"+{add_label_ids} -{remove_label_ids}"
        )
        return {
            "id":        modified["id"],
            "label_ids": modified.get("labelIds", []),
            "status":    "modified",
        }

    except HttpError as exc:
        logger.error(f"[Gmail/label] gmail_modify_message({message_id}) failed: {exc}")
        raise
    except Exception as exc:
        logger.error(f"[Gmail/label] Unexpected error: {exc}")
        raise


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _extract_attachment_metadata(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Recursively walk a Gmail message payload and collect attachment metadata.
    Returns list of dicts with keys: attachment_id, filename, mime_type, size.
    Parts with no attachmentId are body parts — skipped.
    """
    attachments: List[Dict[str, Any]] = []

    def _walk(part: Dict[str, Any]) -> None:
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        filename = part.get("filename", "")
        if attachment_id and filename:
            attachments.append({
                "attachment_id": attachment_id,
                "filename":      filename,
                "mime_type":     part.get("mimeType", ""),
                "size":          body.get("size", 0),
            })
        for sub in part.get("parts", []):
            _walk(sub)

    _walk(payload)
    return attachments


def _extract_message_body(payload: Dict[str, Any]) -> str:
    """
    Extract message body from Gmail message payload

    Args:
        payload: Gmail message payload

    Returns:
        Decoded message body
    """
    body = ""

    if 'body' in payload and 'data' in payload['body']:
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
    elif 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain':
                if 'data' in part.get('body', {}):
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                    break

    return body


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_gmail_tools(tool_registry):
    """
    Register all Gmail tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # gmail_search_threads
    tool_registry.register_tool(
        name="gmail_search_threads",
        function=gmail_search_threads,
        description="Search Gmail threads using Gmail search query syntax. Returns list of thread IDs and snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query (e.g., 'from:john@example.com', 'subject:meeting', 'is:unread')"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of threads to return (default: 10)",
                    "default": 10
                }
            },
            "required": ["query"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # gmail_get_thread
    tool_registry.register_tool(
        name="gmail_get_thread",
        function=gmail_get_thread,
        description="Get full content of a Gmail thread by ID, including all messages in the thread.",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Gmail thread ID"
                }
            },
            "required": ["thread_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # gmail_send_message
    tool_registry.register_tool(
        name="gmail_send_message",
        function=gmail_send_message,
        description="Send a new Gmail message. Can be a reply to an existing thread or a new conversation.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text or HTML)"},
                "thread_id": {"type": "string", "description": "Optional: Thread ID to reply to"},
                "cc": {"type": "string", "description": "Optional: CC email addresses (comma-separated)"},
                "bcc": {"type": "string", "description": "Optional: BCC email addresses (comma-separated)"}
            },
            "required": ["to", "subject", "body"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # gmail_create_draft
    tool_registry.register_tool(
        name="gmail_create_draft",
        function=gmail_create_draft,
        description="Create a Gmail draft message without sending it.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body (plain text or HTML)"},
                "cc": {"type": "string", "description": "Optional: CC email addresses (comma-separated)"}
            },
            "required": ["to", "subject", "body"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # gmail_modify_thread
    tool_registry.register_tool(
        name="gmail_modify_thread",
        function=gmail_modify_thread,
        description="Modify labels on a Gmail thread (add/remove labels like INBOX, UNREAD, STARRED, etc.)",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "Gmail thread ID"},
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to add (e.g., ['STARRED', 'IMPORTANT'])"
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels to remove (e.g., ['UNREAD', 'INBOX'])"
                }
            },
            "required": ["thread_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # gmail_list_labels
    tool_registry.register_tool(
        name="gmail_list_labels",
        function=gmail_list_labels,
        description="List all Gmail labels (both system and user-created labels).",
        parameters={
            "type": "object",
            "properties": {}
        },
        requires_auth=True,
        auth_type="oauth"
    )

    logger.info("Gmail tools registered successfully")
