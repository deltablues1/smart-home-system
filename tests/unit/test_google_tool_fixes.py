"""
Unit tests for the Google-tool correctness/safety fixes:

- Drive delete moves to trash instead of permanent files().delete()
- Tasks list wrapper passes max_results/show_completed as keywords
- Marketing tools report not_implemented instead of fabricated success
- Calendar: timezone body, start<end validation, attendees passthrough,
  delete confirmation gate
- Gmail: attachment path sandbox
- Contacts: ambiguous name resolution, email merge keeps other addresses
- Docs: no empty orphan document when content insertion fails

All Google APIs are mocked — no network.

Run with:
    pytest tests/unit/test_google_tool_fixes.py -v
"""

import asyncio
from unittest.mock import MagicMock

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def fake_creds():
    creds = MagicMock()
    creds.valid = True
    return creds


# ---------------------------------------------------------------------------
# Drive: delete -> trash
# ---------------------------------------------------------------------------

class TestDriveTrash:
    @pytest.mark.asyncio
    async def test_delete_moves_to_trash(self, monkeypatch, fake_creds):
        import tools.api_implementations.drive_api as drive_api

        service = MagicMock()
        api_client = MagicMock()
        api_client.drive_service.return_value = service
        monkeypatch.setattr(
            "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
        )

        async def fake_aexecute(request):
            return {}

        monkeypatch.setattr(drive_api, "aexecute", fake_aexecute)

        result = await drive_api.drive_delete_file(fake_creds, "file123")

        service.files().update.assert_called_with(
            fileId="file123", body={"trashed": True}
        )
        service.files().delete.assert_not_called()
        assert result["status"] == "trashed"


# ---------------------------------------------------------------------------
# Tasks: argument order
# ---------------------------------------------------------------------------

class TestTasksArgs:
    @pytest.mark.asyncio
    async def test_list_passes_keywords(self, monkeypatch, fake_creds):
        import tools.adk_tools.tasks_adk_tools as tasks_adk
        import tools.api_implementations.tasks_api as tasks_api

        captured = {}

        async def fake_impl(creds, tasklist_id="@default", max_results=100,
                            show_completed=False, show_hidden=False):
            captured["max_results"] = max_results
            captured["show_completed"] = show_completed
            return {"tasks": [], "count": 0}

        monkeypatch.setattr(tasks_api, "tasks_list_tasks", fake_impl)
        monkeypatch.setattr(tasks_adk, "_get_credentials", lambda: fake_creds)

        await tasks_adk.tasks_list_tasks(
            tasklist_id="@default", show_completed=True, max_results=7
        )

        assert captured["max_results"] == 7
        assert captured["show_completed"] is True


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

class TestCalendarFixes:
    @pytest.mark.asyncio
    async def test_create_rejects_end_before_start(self, fake_creds):
        from tools.api_implementations.calendar_api import calendar_create_event

        result = await calendar_create_event(
            fake_creds, "Meeting",
            "2026-07-15T15:00:00+02:00", "2026-07-15T14:00:00+02:00",
        )
        assert result["status"] == "error"
        assert "must be after" in result["error"]

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_time(self, fake_creds):
        from tools.api_implementations.calendar_api import calendar_create_event

        result = await calendar_create_event(
            fake_creds, "Meeting", "not-a-time", "2026-07-15T14:00:00+02:00"
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_create_uses_user_timezone(self, monkeypatch, fake_creds):
        import tools.api_implementations.calendar_api as calendar_api

        service = MagicMock()
        api_client = MagicMock()
        api_client.calendar_service.return_value = service
        monkeypatch.setattr(
            "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
        )

        async def fake_aexecute(request):
            return {
                "id": "e1", "summary": "Meeting",
                "start": {"dateTime": "2026-07-15T14:00:00+02:00"},
                "end": {"dateTime": "2026-07-15T15:00:00+02:00"},
                "htmlLink": "https://cal",
            }

        monkeypatch.setattr(calendar_api, "aexecute", fake_aexecute)

        await calendar_api.calendar_create_event(
            fake_creds, "Meeting",
            "2026-07-15T14:00:00+02:00", "2026-07-15T15:00:00+02:00",
        )

        body = service.events().insert.call_args.kwargs["body"]
        assert body["start"]["timeZone"] == "Europe/Zagreb"
        assert body["end"]["timeZone"] == "Europe/Zagreb"

    @pytest.mark.asyncio
    async def test_update_wrapper_passes_attendees(self, monkeypatch, fake_creds):
        import tools.adk_tools.calendar_adk_tools as calendar_adk
        import tools.api_implementations.calendar_api as calendar_api

        captured = {}

        async def fake_impl(creds, event_id, summary=None, start_time=None,
                            end_time=None, description=None, location=None,
                            attendees=None, calendar_id="primary"):
            captured["attendees"] = attendees
            return {"id": event_id, "status": "updated"}

        monkeypatch.setattr(calendar_api, "calendar_update_event", fake_impl)
        monkeypatch.setattr(calendar_adk, "_get_credentials", lambda: fake_creds)

        await calendar_adk.calendar_update_event(
            "evt1", attendees=["ana@example.com"]
        )
        assert captured["attendees"] == ["ana@example.com"]

    @pytest.mark.asyncio
    async def test_delete_requires_confirmation(self, monkeypatch, fake_creds):
        import tools.adk_tools.calendar_adk_tools as calendar_adk
        import tools.api_implementations.calendar_api as calendar_api

        delete_called = {"value": False}

        async def fake_delete(creds, event_id, calendar_id="primary"):
            delete_called["value"] = True
            return {"id": event_id, "status": "deleted"}

        async def fake_get(creds, event_id, calendar_id="primary"):
            return {"id": event_id, "summary": "Sastanak"}

        monkeypatch.setattr(calendar_api, "calendar_delete_event", fake_delete)
        monkeypatch.setattr(calendar_api, "calendar_get_event", fake_get)
        monkeypatch.setattr(calendar_adk, "_get_credentials", lambda: fake_creds)

        result = await calendar_adk.calendar_delete_event("evt1")
        assert result["status"] == "needs_confirmation"
        assert result["summary"] == "Sastanak"
        assert delete_called["value"] is False

        result = await calendar_adk.calendar_delete_event("evt1", confirm=True)
        assert result["status"] == "deleted"
        assert delete_called["value"] is True


# ---------------------------------------------------------------------------
# Gmail: attachment sandbox
# ---------------------------------------------------------------------------

class TestGmailAttachmentSandbox:
    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_dirs(self, monkeypatch, fake_creds):
        import tools.api_implementations.gmail_api as gmail_api

        api_client = MagicMock()
        api_client.gmail_service.return_value = MagicMock()
        monkeypatch.setattr(
            "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
        )

        result = await gmail_api.gmail_send_message(
            fake_creds, "x@example.com", "Subject", "Body",
            attachment_path=".env",
        )
        assert result["status"] == "failed"
        assert "not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_absolute_path_outside(self, monkeypatch, fake_creds):
        import tools.api_implementations.gmail_api as gmail_api

        api_client = MagicMock()
        api_client.gmail_service.return_value = MagicMock()
        monkeypatch.setattr(
            "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
        )

        result = await gmail_api.gmail_send_message(
            fake_creds, "x@example.com", "Subject", "Body",
            attachment_path="C:\\Windows\\system.ini",
        )
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_doomed_send_does_not_share_linked_docs(self, monkeypatch, fake_creds):
        # Attachment validation must run BEFORE doc auto-sharing: a send that
        # cannot succeed must not leave documents shared with the recipient.
        import tools.adk_tools.gmail_adk_tools as gmail_adk

        share_called = {"value": False}

        async def fake_autoshare(creds, body, recipients):
            share_called["value"] = True
            return [], []  # (warnings, grants)

        monkeypatch.setattr(gmail_adk, "_autoshare_linked_docs", fake_autoshare)
        monkeypatch.setattr(gmail_adk, "_get_credentials", lambda: fake_creds)

        result = await gmail_adk.gmail_send_message(
            to="x@example.com", subject="S",
            body="https://docs.google.com/document/d/abc123def456ghi789jkl/edit",
            attachment_path=".env",
        )
        assert result["status"] == "failed"
        assert share_called["value"] is False

    def test_send_has_no_retry_decorator(self):
        # Retrying a non-idempotent send can duplicate emails; ensure the
        # retry wrapper is gone (it exposes a `retry_config` closure cell).
        import inspect
        import tools.api_implementations.gmail_api as gmail_api

        source = inspect.getsource(gmail_api.gmail_send_message)
        decorator_lines = [
            line.strip()
            for line in source.split("async def", 1)[0].splitlines()
            if line.strip().startswith("@")
        ]
        assert not any(d.startswith("@with_retry") for d in decorator_lines)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

class TestContactsDisambiguation:
    @pytest.mark.asyncio
    async def test_ambiguous_returns_candidates(self, monkeypatch):
        import tools.adk_tools.contacts_adk_tools as contacts_adk

        async def fake_search(query, max_results=5):
            return {
                "count": 2,
                "contacts": [
                    {"name": "Ivan Horvat", "email": "ivan.h@example.com"},
                    {"name": "Ivan Kovač", "email": "ivan.k@example.com"},
                ],
            }

        monkeypatch.setattr(contacts_adk, "contacts_search_people", fake_search)

        result = await contacts_adk.contacts_get_by_name("Ivan")
        assert result["found"] is False
        assert result["status"] == "ambiguous"
        assert len(result["candidates"]) == 2

    @pytest.mark.asyncio
    async def test_single_match_returned(self, monkeypatch):
        import tools.adk_tools.contacts_adk_tools as contacts_adk

        async def fake_search(query, max_results=5):
            return {
                "count": 1,
                "contacts": [{"name": "Ana Anić", "email": "ana@example.com"}],
            }

        monkeypatch.setattr(contacts_adk, "contacts_search_people", fake_search)

        result = await contacts_adk.contacts_get_by_name("Ana")
        assert result["found"] is True
        assert result["email"] == "ana@example.com"


class TestContactsEmailMerge:
    @pytest.mark.asyncio
    async def test_update_keeps_other_emails(self, monkeypatch, fake_creds):
        import tools.api_implementations.contacts_api as contacts_api

        service = MagicMock()
        api_client = MagicMock()
        api_client.people_service.return_value = service
        monkeypatch.setattr(
            "tools.google_api_client.GoogleAPIClient", lambda credentials: api_client
        )

        current_person = {
            "etag": "e",
            "names": [{"givenName": "Ana", "familyName": "Anić"}],
            "emailAddresses": [
                {"value": "old@example.com", "type": "work"},
                {"value": "second@example.com", "type": "home"},
            ],
            "phoneNumbers": [],
            "organizations": [],
        }

        calls = {"n": 0}

        async def fake_aexecute(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return current_person
            return {
                "resourceName": "people/c1",
                "etag": "e",
                "names": [{"displayName": "Ana Anić"}],
            }

        monkeypatch.setattr(contacts_api, "aexecute", fake_aexecute)

        await contacts_api.contacts_update_contact(
            fake_creds, "people/c1", email="new@example.com"
        )

        body = service.people().updateContact.call_args.kwargs["body"]
        emails = [e["value"] for e in body["emailAddresses"]]
        assert emails == ["new@example.com", "second@example.com"]
        # secondary label preserved
        assert body["emailAddresses"][1]["type"] == "home"


# ---------------------------------------------------------------------------
# Docs: orphan cleanup
# ---------------------------------------------------------------------------

class TestDocsOrphanCleanup:
    @pytest.mark.asyncio
    async def test_failed_insert_trashes_empty_doc(self, monkeypatch, fake_creds):
        import tools.adk_tools.docs_adk_tools as docs_adk
        import tools.api_implementations.docs_api as docs_api
        import tools.api_implementations.drive_api as drive_api

        trashed = {"ids": []}

        async def fake_create(creds, title, content=None):
            return {"document_id": "doc1", "title": title}

        async def fake_batch(creds, doc_id, requests):
            raise RuntimeError("batch failed")

        async def fake_insert(creds, doc_id, text, index):
            raise RuntimeError("insert failed")

        async def fake_trash(creds, file_id):
            trashed["ids"].append(file_id)
            return {"id": file_id, "status": "trashed"}

        monkeypatch.setattr(docs_api, "docs_create_document", fake_create)
        monkeypatch.setattr(docs_api, "docs_batch_update", fake_batch)
        monkeypatch.setattr(docs_api, "docs_insert_text", fake_insert)
        monkeypatch.setattr(drive_api, "drive_delete_file", fake_trash)
        monkeypatch.setattr(docs_adk, "_get_credentials", lambda: fake_creds)

        result = await docs_adk.docs_create_document("Naslov", content="# Sadržaj")

        assert result["status"] == "error"
        assert result["cleaned_up"] is True
        assert trashed["ids"] == ["doc1"]

    @pytest.mark.asyncio
    async def test_successful_insert_no_cleanup(self, monkeypatch, fake_creds):
        import tools.adk_tools.docs_adk_tools as docs_adk
        import tools.api_implementations.docs_api as docs_api

        async def fake_create(creds, title, content=None):
            return {"document_id": "doc1", "title": title,
                    "document_url": "https://docs/doc1"}

        async def fake_batch(creds, doc_id, requests):
            return {"replies": []}

        monkeypatch.setattr(docs_api, "docs_create_document", fake_create)
        monkeypatch.setattr(docs_api, "docs_batch_update", fake_batch)
        monkeypatch.setattr(docs_adk, "_get_credentials", lambda: fake_creds)

        result = await docs_adk.docs_create_document("Naslov", content="# Sadržaj")
        assert result["content_inserted"] is True
        assert "error" not in result
