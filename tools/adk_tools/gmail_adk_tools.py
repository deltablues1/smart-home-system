"""
Gmail ADK Tools

ADK-compatible wrappers for Gmail operations.
"""

from typing import Optional, List
import re
import logging

from tools.resilience.retry_handler import UnconfirmedWrite
logger = logging.getLogger(__name__)


# ============================================================================
# AUTHENTICATION HELPER
# ============================================================================

def _get_credentials():
    """Get OAuth credentials from token file"""
    try:
        from auth.oauth_manager import get_oauth_manager
        oauth_manager = get_oauth_manager()
        creds = oauth_manager.get_credentials()
        if creds and creds.valid:
            return creds
        logger.warning("No valid credentials available for Gmail operations")
        return None
    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


# ============================================================================
# RECIPIENT-SCOPED DOC SHARING (least privilege)
# ============================================================================
# When an outgoing email links a Google Doc/Drive file, the recipient must be
# able to open it. Rather than making the document public ("anyone with link"),
# we share it directly with the actual recipient(s) at send time. This is
# deterministic (runs in code, not at the LLM's discretion) and least-privilege.

# Shared with the approval gate, which has to name the same documents in its
# question and fold them into the fingerprint. Two copies of this regex would
# eventually disagree, and the disagreement would be a mail approved for one
# set of documents going out sharing another.
from services.drive_links import extract_file_ids as _extract_drive_file_ids


def _parse_recipients(*fields: Optional[str]) -> List[str]:
    """Flatten comma/semicolon-separated to/cc fields into unique email addresses."""
    emails: List[str] = []
    for field in fields:
        if not field:
            continue
        for part in str(field).replace(";", ",").split(","):
            addr = part.strip()
            if "@" in addr and addr not in emails:
                emails.append(addr)
    return emails


async def _autoshare_linked_docs(creds, body: str, recipients: List[str]):
    """Share any Google Doc/Drive file linked in *body* with *recipients* (reader).

    Returns `(warnings, grants)`. Each grant is a permission THIS call created,
    with the id needed to take it back. The old code threw that id away and
    explained in a comment that drive_share_file did not return one, which was
    not true.

    Recipients who already have access are skipped, so a recorded grant is one
    we created rather than someone's pre-existing access. That is a strong
    guide, not a proof: reading the current permissions and creating a new one
    are two calls with a gap between them, so a share made by somebody else in
    that gap would still be recorded as ours. It is the best evidence
    available without a transaction, and it is only ever used to undo a share
    made moments earlier for a send that provably failed.

    Best-effort: failures never block the send, but they come back as warnings
    so the caller can tell the user a linked doc may not open.
    """
    warnings: List[str] = []
    grants: List[dict] = []
    file_ids = _extract_drive_file_ids(body)
    if not file_ids or not recipients:
        return warnings, grants
    try:
        from tools.api_implementations.drive_api import (
            drive_list_permissions as drive_permissions_impl,
            drive_share_file as drive_share_impl,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[autoshare] drive_api import failed: {e}")
        return (
            [f"Could not share linked documents (drive_api unavailable: {e})"],
            grants,
        )

    for fid in file_ids:
        already = await _existing_readers(drive_permissions_impl, creds, fid)
        for email in recipients:
            if already is not None and email.lower() in already:
                logger.info(f"[autoshare] {email} already has access to {fid}")
                continue
            try:
                # send_notification=False: we are about to email the recipient
                # ourselves, so suppress Drive own "shared with you" mail.
                result = await drive_share_impl(
                    creds, fid, email, "reader", "user", send_notification=False
                )
                logger.info(f"[autoshare] shared {fid} with {email} (reader, no notify)")
                permission_id = (result or {}).get("permission_id")
                # `already is None` means the current access could not be read,
                # so ours cannot be told apart from theirs afterwards. Share,
                # but never offer to undo it.
                if permission_id and already is not None:
                    grants.append({
                        "file_id": fid,
                        "email": email,
                        "permission_id": permission_id,
                    })
            except Exception as e:
                logger.warning(f"[autoshare] could not share {fid} with {email}: {e}")
                warnings.append(
                    f"Linked document {fid} could not be shared with {email} — "
                    "the recipient may get 'access denied' when opening the link."
                )
    return warnings, grants


async def _existing_readers(list_permissions, creds, file_id):
    """Lower-cased addresses that already have access, or None if unreadable."""
    try:
        result = await list_permissions(creds, file_id)
    except Exception as e:
        logger.warning(f"[autoshare] could not read permissions on {file_id}: {e}")
        return None
    addresses = set()
    for permission in (result or {}).get("permissions", []):
        address = (permission.get("emailAddress") or "").strip().lower()
        if address:
            addresses.add(address)
    return addresses


async def _revoke_grants(creds, grants: List[dict]) -> List[str]:
    """Undo access this send granted, once the send provably did not happen."""
    undone: List[str] = []
    if not grants:
        return undone
    try:
        from tools.api_implementations.drive_api import (
            drive_revoke_permission as drive_revoke_impl,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[autoshare] revoke unavailable: {e}")
        return undone
    for grant in grants:
        try:
            await drive_revoke_impl(creds, grant["file_id"], grant["permission_id"])
            undone.append("{} -> {}".format(grant["file_id"], grant["email"]))
        except Exception as e:
            logger.warning(
                "[autoshare] could not revoke %s on %s: %s",
                grant["permission_id"], grant["file_id"], e,
            )
    return undone


def _describe_grants(grants: List[dict]) -> str:
    return ", ".join("{} -> {}".format(g["file_id"], g["email"]) for g in grants)


def _proves_not_sent(error: Exception) -> bool:
    """Can we prove the message never went out?

    Only a rejection proves it: Gmail understood the request and refused it,
    so nothing was delivered. Everything else may have happened anyway — a
    timeout, a dropped connection, and also the quieter ones, a response that
    would not parse or a field missing from a reply that arrived because the
    send succeeded.

    Asked in this direction on purpose. The first version asked "is this a
    known transient error", which made every UNRECOGNISED error a reason to
    revoke — so a JSONDecodeError after a successful send would have taken the
    document away from someone already reading the mail.
    """
    try:
        from googleapiclient.errors import HttpError

        if isinstance(error, HttpError):
            status = getattr(getattr(error, "resp", None), "status", None)
            # 4xx is a refusal; 429 is "later", which is not the same thing.
            return isinstance(status, int) and 400 <= status < 500 and status != 429
    except Exception:  # pragma: no cover - googleapiclient not importable
        pass
    return False


async def _undo_shares(creds, grants: List[dict], result: dict) -> None:
    """Take back the access this send granted, and say so in the result."""
    if not grants:
        return
    undone = await _revoke_grants(creds, grants)
    if undone:
        result["share_warning"] = (
            result.get("share_warning", "") +
            " Mail nije poslan, pa sam povukao pristup koji sam za njega "
            "dodijelio: " + ", ".join(undone) + "."
        ).strip()
    else:
        result["share_warning"] = (
            result.get("share_warning", "") +
            " NAPOMENA: mail nije poslan, a pristup dokumentima nisam uspio "
            "povući: " + _describe_grants(grants) + "."
        ).strip()


# ============================================================================
# GMAIL TOOLS
# ============================================================================

async def gmail_search_threads(query: str, max_results: int = 10) -> dict:
    """
    Search Gmail threads using Gmail search query syntax.

    Use this to find emails matching specific criteria. Supports Gmail's
    powerful search operators like 'from:', 'subject:', 'is:unread', etc.

    Args:
        query: Gmail search query. Examples:
               - 'from:john@example.com' - emails from specific sender
               - 'subject:meeting' - emails with meeting in subject
               - 'is:unread' - unread emails
               - 'has:attachment' - emails with attachments
        max_results: Maximum number of threads to return (default: 10)

    Returns:
        Dictionary containing:
            - threads: List of thread objects with id, snippet, from, to, subject
            - result_size_estimate: Total number of matching threads
            - query: The search query used
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.gmail_api import gmail_search_threads as gmail_search_impl
        result = await gmail_search_impl(creds, query, max_results)
        logger.info(f"Searched Gmail: '{query}' - found {result.get('result_size_estimate', 0)} threads")
        return result
    except Exception as e:
        logger.error(f"gmail_search_threads failed: {e}")
        return {"error": str(e), "status": "error"}


async def gmail_get_thread(thread_id: str) -> dict:
    """
    Get full content of a Gmail thread by ID.

    Retrieves all messages in a thread including full email bodies.

    Args:
        thread_id: Gmail thread ID (obtained from gmail_search_threads)

    Returns:
        Dictionary containing:
            - id: Thread ID
            - messages: List of message objects with from, to, subject, body
            - message_count: Number of messages in thread
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.gmail_api import gmail_get_thread as gmail_get_thread_impl
        result = await gmail_get_thread_impl(creds, thread_id)
        logger.info(f"Retrieved Gmail thread: {thread_id}")
        return result
    except Exception as e:
        logger.error(f"gmail_get_thread failed: {e}")
        return {"error": str(e), "status": "error"}


async def gmail_send_message(
    to: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachment_path: Optional[str] = None
) -> dict:
    """
    Send a Gmail message or reply to existing thread, with optional file attachment.

    IMPORTANT: 'to' must be a valid email address (user@domain.com).

    Args:
        to: Recipient email address (REQUIRED, must contain @)
        subject: Email subject line (REQUIRED)
        body: Email body content, can be plain text or HTML (REQUIRED)
        thread_id: Optional thread ID to reply to (makes this a reply)
        cc: Optional CC email addresses (comma-separated)
        bcc: Optional BCC email addresses (comma-separated)
        attachment_path: Optional local file path to attach (e.g., "output/invoices/invoice_1_1_1_abc.pdf")

    Returns:
        Dictionary containing:
            - id: Message ID
            - threadId: Thread ID
            - status: Success message

    Example:
        result = await gmail_send_message(
            to="recipient@example.com",
            subject="Mjesečni izvještaj",
            body="U prilogu se nalazi izvještaj.",
            attachment_path="output/reports/report_2026_09.pdf"
        )
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    # Validate email format
    if "@" not in to:
        return {
            "error": f"Invalid recipient email: '{to}'. Must be valid email address (user@domain.com)",
            "status": "invalid_email"
        }

    # Validate the attachment BEFORE auto-sharing linked docs: a send that is
    # doomed to fail must not leave documents shared with the recipients.
    if attachment_path:
        from tools.api_implementations.gmail_api import validate_attachment_path

        _, path_error = validate_attachment_path(attachment_path)
        if path_error:
            return {"error": path_error, "status": "failed"}

    # Least-privilege: share any linked Google Doc/Drive file with the actual
    # recipients before sending, so the emailed link opens (no public sharing).
    share_warnings, grants = await _autoshare_linked_docs(
        creds, body, _parse_recipients(to, cc, bcc)
    )

    # Three outcomes, not two. A send that was refused did not happen, so the
    # access granted for it can be taken back. A send whose answer was lost may
    # well have gone out, and revoking then takes the document away from
    # someone already reading the mail — so those grants stay, and the user is
    # told what is unresolved.
    try:
        from tools.api_implementations.gmail_api import gmail_send_message as gmail_send_impl
        result = await gmail_send_impl(creds, to, subject, body, thread_id, cc, bcc, attachment_path)
        logger.info(f"Sent Gmail message to {to}: '{subject}'" + (f" with attachment: {attachment_path}" if attachment_path else ""))
        if isinstance(result, dict):
            if share_warnings:
                result["share_warning"] = " ".join(share_warnings)
            if result.get("status") == "failed":
                await _undo_shares(creds, grants, result)
        return result
    except Exception as e:
        logger.error(f"gmail_send_message failed: {e}")
        failure = {"error": str(e), "status": "error"}
        if share_warnings:
            failure["share_warning"] = " ".join(share_warnings)
        if _proves_not_sent(e):
            await _undo_shares(creds, grants, failure)
        else:
            failure["outcome"] = "unknown"
            if grants:
                failure["share_warning"] = (
                    failure.get("share_warning", "") +
                    " NAPOMENA: ne znam je li mail poslan, pa NISAM povukao "
                    "pristup dokumentima: " + _describe_grants(grants) + "."
                ).strip()
        return failure


async def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None
) -> dict:
    """
    Create a Gmail draft message without sending it.

    Use this for important emails that need review before sending.

    Args:
        to: Recipient email address (REQUIRED, must contain @)
        subject: Email subject line (REQUIRED)
        body: Email body content (REQUIRED)
        cc: Optional CC email addresses (comma-separated)

    Returns:
        Dictionary containing:
            - id: Draft ID
            - message: Draft message object
            - status: Success message
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    # Validate email format
    if "@" not in to:
        return {
            "error": f"Invalid recipient email: '{to}'. Must be valid email address",
            "status": "invalid_email"
        }

    # Deliberately NO doc auto-sharing here: a draft may never be sent, and
    # sharing at draft time would leak access prematurely. Linked docs are
    # shared at send time (gmail_send_message); drafts sent manually from the
    # Gmail UI need manual sharing.
    try:
        from tools.api_implementations.gmail_api import gmail_create_draft as gmail_create_draft_impl
        result = await gmail_create_draft_impl(creds, to, subject, body, cc)
        logger.info(f"Created Gmail draft to {to}: '{subject}'")
        if isinstance(result, dict) and _extract_drive_file_ids(body):
            result["share_note"] = (
                "Draft contains Drive/Docs links. They are NOT shared yet — "
                "sharing happens automatically only when sending via this "
                "system. If the user sends the draft manually from Gmail, "
                "the documents must be shared manually."
            )
        return result
    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in gmail_create_draft: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"gmail_create_draft failed: {e}")
        return {"error": str(e), "status": "error"}


async def gmail_modify_thread(
    thread_id: str,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None
) -> dict:
    """
    Modify labels on a Gmail thread.

    Use this to organize emails by adding or removing Gmail labels.

    Common system labels: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH

    Args:
        thread_id: Gmail thread ID (REQUIRED)
        add_labels: List of label names to add (e.g., ['STARRED', 'IMPORTANT'])
        remove_labels: List of label names to remove (e.g., ['UNREAD', 'INBOX'])

    Returns:
        Dictionary containing:
            - id: Thread ID
            - labelIds: Updated list of labels
            - status: Success message
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.gmail_api import gmail_modify_thread as gmail_modify_thread_impl
        result = await gmail_modify_thread_impl(creds, thread_id, add_labels, remove_labels)
        logger.info(f"Modified Gmail thread: {thread_id}")
        return result
    except Exception as e:
        logger.error(f"gmail_modify_thread failed: {e}")
        return {"error": str(e), "status": "error"}


async def gmail_list_labels() -> dict:
    """
    List all Gmail labels.

    Retrieves both system labels (INBOX, SENT, etc.) and user-created labels.

    Returns:
        Dictionary containing:
            - labels: List of label objects with id, name, type
            - count: Number of labels
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.gmail_api import gmail_list_labels as gmail_list_labels_impl
        result = await gmail_list_labels_impl(creds)
        logger.info(f"Listed {result.get('count', 0)} Gmail labels")
        return result
    except Exception as e:
        logger.error(f"gmail_list_labels failed: {e}")
        return {"error": str(e), "status": "error"}
