"""
Media Service for handling image/file uploads and serving.

Stores files locally (uploads/) for simplicity.
Can be extended to use GCS via vertex_ai.save_to_gcs() for production.
"""

import os
import uuid
import base64
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Local upload directory
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Allowed MIME types for upload
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp",
    "application/pdf",
    "video/mp4", "video/webm",
}

# Max file size: 20MB (Gemini inline limit)
MAX_FILE_SIZE = 20 * 1024 * 1024

# What the bytes actually are, not what the upload claimed they are.
# content_type comes from the client and is trivially forged, so a file
# announcing itself as image/png is checked against the signature of the
# formats we accept before anything downstream tries to interpret it.
# Byte values are written numerically to keep the escapes out of the way.
_PNG_MAGIC = bytes([0x89]) + b'PNG' + bytes([0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF])
_WEBM_MAGIC = bytes([0x1A, 0x45, 0xDF, 0xA3])

_MAGIC_SIGNATURES = (
    (_JPEG_MAGIC, 'image/jpeg'),
    (_PNG_MAGIC, 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
    (b'BM', 'image/bmp'),
    (b'%PDF-', 'application/pdf'),
    (_WEBM_MAGIC, 'video/webm'),
)


def sniff_mime(head: bytes) -> Optional[str]:
    """Best-effort format detection from the first bytes. None = unrecognised."""
    for signature, mime in _MAGIC_SIGNATURES:
        if head.startswith(signature):
            return mime
    # RIFF containers carry their real type at offset 8.
    if head[:4] == b'RIFF' and len(head) >= 12 and head[8:12] == b'WEBP':
        return 'image/webp'
    # ISO base media (mp4) puts 'ftyp' at offset 4.
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return 'video/mp4'
    return None



def save_upload(file_bytes: bytes, original_filename: str, mime_type: str) -> dict:
    """
    Save an uploaded file and return metadata.

    Returns:
        dict with file_id, filename, mime_type, size, path
    """
    file_id = uuid.uuid4().hex[:12]
    ext = _ext_from_mime(mime_type) or os.path.splitext(original_filename)[1]
    safe_name = f"{file_id}{ext}"
    filepath = os.path.join(UPLOAD_DIR, safe_name)

    with open(filepath, "wb") as f:
        f.write(file_bytes)

    logger.info(f"Saved upload: {safe_name} ({len(file_bytes)} bytes, {mime_type})")

    return {
        "file_id": file_id,
        "filename": safe_name,
        "original_name": original_filename,
        "mime_type": mime_type,
        "size": len(file_bytes),
        "path": filepath,
    }


def get_file_path(file_id: str) -> Optional[str]:
    """Find file by ID in uploads directory.

    Filenames are either '{file_id}{ext}' (uploads) or '{prefix}_{file_id}.png'
    (generated images), so match the stem exactly — a substring match could
    return the wrong file for short or overlapping IDs.
    """
    if not file_id or not file_id.isalnum():
        return None
    for fname in os.listdir(UPLOAD_DIR):
        stem = os.path.splitext(fname)[0]
        if stem == file_id or stem.endswith(f"_{file_id}"):
            return os.path.join(UPLOAD_DIR, fname)
    return None


def get_file_base64(file_id: str) -> Optional[Tuple[str, str]]:
    """
    Get file as base64 string + mime type.
    Returns (base64_data, mime_type) or None.
    """
    path = get_file_path(file_id)
    if not path:
        return None

    mime_type = _mime_from_ext(os.path.splitext(path)[1])
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")

    return data, mime_type


def save_generated_image(image_bytes: bytes, prefix: str = "generated") -> dict:
    """
    Save an agent-generated image (e.g., from Imagen/Gemini).

    Returns dict with file_id, filename, url_path (for serving via API).
    """
    file_id = uuid.uuid4().hex[:12]
    filename = f"{prefix}_{file_id}.png"
    filepath = os.path.join(UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(image_bytes)

    logger.info(f"Saved generated image: {filename} ({len(image_bytes)} bytes)")

    return {
        "file_id": file_id,
        "filename": filename,
        "url_path": f"/api/media/{file_id}",
        "size": len(image_bytes),
    }


def _ext_from_mime(mime_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "application/pdf": ".pdf",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    return mapping.get(mime_type, "")


def _mime_from_ext(ext: str) -> str:
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }
    return mapping.get(ext.lower(), "application/octet-stream")
