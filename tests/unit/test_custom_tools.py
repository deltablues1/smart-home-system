"""
Unit Tests for Custom Tools
"""

import pytest
from tools.custom_tools.docs_formatter import DocsFormatter, format_markdown_for_docs
from tools.custom_tools.drive_query_translator import DriveQueryTranslator, translate_drive_query


@pytest.mark.unit
class TestDocsFormatter:
    """Test Docs Formatter tool"""

    def test_docs_formatter_init(self):
        """Test DocsFormatter initialization"""
        formatter = DocsFormatter()
        assert formatter.requests == []
        assert formatter.current_index == 1

    def test_simple_heading(self):
        """Test simple heading conversion"""
        formatter = DocsFormatter()
        markdown = "# Heading 1"
        requests = formatter.markdown_to_docs_requests(markdown)
        
        assert len(requests) >= 2  # Insert + style
        assert any('insertText' in req for req in requests)
        assert any('updateParagraphStyle' in req for req in requests)

    def test_bold_text(self):
        """Test bold text formatting"""
        markdown = "This is **bold** text"
        requests = format_markdown_for_docs(markdown)
        assert len(requests) > 0

    def test_list_formatting(self):
        """Test list item formatting"""
        formatter = DocsFormatter()
        markdown = "- Item 1\n- Item 2"
        requests = formatter.markdown_to_docs_requests(markdown)
        
        assert len(requests) > 0
        # Should have createParagraphBullets requests
        assert any('createParagraphBullets' in req for req in requests)

    def test_empty_markdown(self):
        """Test empty markdown handling"""
        requests = format_markdown_for_docs("")
        assert isinstance(requests, list)


@pytest.mark.unit
class TestDriveQueryTranslator:
    """Test Drive Query Translator"""

    def test_translator_init(self):
        """Test DriveQueryTranslator initialization"""
        translator = DriveQueryTranslator()
        assert translator.MIME_TYPES is not None

    def test_simple_name_search(self):
        """Test simple file name search"""
        query = translate_drive_query("find budget")
        assert "name contains 'budget'" in query
        assert "trashed = false" in query

    def test_file_type_search(self):
        """Test file type search"""
        query = translate_drive_query("find spreadsheets")
        assert "mimeType" in query
        assert "spreadsheet" in query.lower()

    def test_time_range_search(self):
        """Test time-based search"""
        query = translate_drive_query("files from last week")
        assert "modifiedTime >" in query

    def test_my_files_search(self):
        """Test ownership search"""
        query = translate_drive_query("my presentations")
        assert "'me' in owners" in query
        assert "presentation" in query.lower()

    def test_starred_files(self):
        """Test starred files search"""
        query = translate_drive_query("starred documents")
        assert "starred = true" in query

    def test_complex_query(self):
        """Test complex multi-criteria query"""
        query = translate_drive_query("my spreadsheets from last month")
        assert "'me' in owners" in query
        assert "mimeType" in query
        assert "modifiedTime" in query
