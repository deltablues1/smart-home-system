"""
An upload must not cost the machine anything before it is refused.

The endpoint read the whole file into memory and checked its size afterwards,
which is the wrong order on a Pi: one oversized upload exhausts the RAM before
the check that exists to prevent exactly that. It also trusted the client's
content_type, which establishes nothing — a caller declaring "image/png" can
send whatever it likes.

Run with:
    pytest tests/unit/test_upload_limits.py -v
"""

import pytest

from services.media_service import ALLOWED_MIME_TYPES, MAX_FILE_SIZE, sniff_mime

PNG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
JPEG = bytes([0xFF, 0xD8, 0xFF])
WEBM = bytes([0x1A, 0x45, 0xDF, 0xA3])


class TestSniffing:
    @pytest.mark.parametrize(
        "head,expected",
        [
            (PNG + b"rest", "image/png"),
            (JPEG + b"rest", "image/jpeg"),
            (b"GIF89a...", "image/gif"),
            (b"BM......", "image/bmp"),
            (b"%PDF-1.7", "application/pdf"),
            (b"RIFF????WEBPVP8 ", "image/webp"),
            (b"????ftypisom", "video/mp4"),
            (WEBM + b"rest", "video/webm"),
        ],
    )
    def test_known_formats_are_recognised(self, head, expected):
        assert sniff_mime(head) == expected

    @pytest.mark.parametrize(
        "head",
        [b"MZ\x90\x00", b"#!/bin/sh\n", b"<html>", b"", b"nonsense"],
    )
    def test_anything_else_is_unrecognised(self, head):
        assert sniff_mime(head) is None

    def test_every_sniffable_format_is_an_allowed_one(self):
        """A format we can detect but would not accept is a trap: it would pass
        the sniff check and then be refused, or worse, not refused."""
        for head, mime in [
            (PNG, "image/png"), (JPEG, "image/jpeg"), (b"%PDF-", "application/pdf"),
            (b"RIFF????WEBP", "image/webp"), (b"????ftypisom", "video/mp4"),
        ]:
            assert sniff_mime(head) in ALLOWED_MIME_TYPES


class TestAForgedContentTypeIsCaught:
    def test_an_executable_claiming_to_be_an_image_does_not_sniff_as_one(self):
        assert sniff_mime(b"MZ\x90\x00this is a windows binary") is None

    def test_a_script_claiming_to_be_a_pdf_does_not_sniff_as_one(self):
        assert sniff_mime(b"#!/bin/sh\nrm -rf /") is None


class TestTheLimitIsSane:
    def test_the_cap_is_small_enough_for_a_pi(self):
        assert MAX_FILE_SIZE <= 32 * 1024 * 1024

    def test_the_endpoint_reads_in_chunks(self):
        """Regression guard: a single .read() with no argument is the bug."""
        from pathlib import Path

        app_py = (Path(__file__).resolve().parents[2] / "web" / "app.py").read_text(
            encoding="utf-8"
        )
        upload = app_py.split('@app.post("/api/upload")')[1][:3000]
        assert "await file.read(64 * 1024)" in upload
        assert "await file.read()" not in upload
